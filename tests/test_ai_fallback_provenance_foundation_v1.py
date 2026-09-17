"""Pure, no-I/O coverage for the S5e-A provenance material foundation."""

from __future__ import annotations

import ast
import inspect
from copy import deepcopy

import pytest

from finance_core.parser_proposals import ai_fallback_provenance as provenance

HASH = "0" * 64


def preparation_material() -> dict[str, object]:
    material: dict[str, object] = {}
    for field in provenance.PREPARATION_MATERIAL_FIELDS:
        material[field] = "v1"
    material.update(
        {
            "schema_version": "finance-ai-preparation-material-v1",
            "intake_public_id": "raw_intake_test",
            "parent_public_id": "proposal_test",
            "parent_version": 0,
            "parent_effective_content_hash": HASH,
            "source_kind": "telegram_raw_text",
            "source_projection_hash": HASH,
            "source_selection_manifest_hash": HASH,
            "source_field_state_hash": HASH,
            "eligibility_mode": "child_eligible",
            "eligibility_reasons": [
                "deterministic_fields_incomplete",
                "unsupported_language",
            ],
            "runtime_policy_hash": HASH,
            "prompt_template_hash": HASH,
            "intent_policy_hash": HASH,
            "intent_evidence_hash": HASH,
            "default_policy_hash": HASH,
            "default_evidence_hash": None,
            "sensitive_text_policy_hash": HASH,
            "sensitive_text_scan_hash": HASH,
            "deadline_policy_hash": HASH,
            "sqlite_money_policy_hash": HASH,
            "expected_provider": "synthetic-provider",
            "expected_model": "synthetic/model-v1",
            "expected_agent_id": "finance-test-agent",
            "expected_audit_caller_kind": "plugin",
            "expected_audit_caller_id": "finance-bridge",
            "expected_audit_purpose": "finance-bridge.ai-proposal-v1",
            "expected_audit_caller_name": None,
            "expected_audit_session_key_sha256": None,
            "request_sha256": HASH,
            "request_byte_count": 42,
        }
    )
    return material


def claim_material() -> dict[str, object]:
    return {
        "schema_version": "finance-ai-claim-material-v1",
        "attempt_public_id": "aifa_example",
        "invocation_claimed_at_ms": 1_700_000_000_000,
        "call_start_not_after_ms": 1_700_000_000_250,
        "request_sha256": HASH,
        "invocation_disposition": "invoke_once",
    }


def result_material() -> dict[str, object]:
    material: dict[str, object] = {}
    for field in provenance.RESULT_MATERIAL_FIELDS:
        material[field] = "v1"
    material.update(
        {
            "schema_version": "finance-ai-result-material-v1",
            "attempt_public_id": "aifa_example",
            "claim_public_id": "aicl_example",
            "claim_material_hash": HASH,
            "transport_outcome": "provider_error",
            "result_status": "provider_error",
            "retention_state": "none",
            "normal_attribution_hash": None,
            "metadata_refusal_hash": None,
            "usage_hash": None,
            "result_received_at_ms": 1,
            "post_lock_at_ms": 2,
            "decision_at_ms": 3,
            "deadline_policy_hash": HASH,
            "response_body_state": "none",
            "response_sha256": None,
            "response_byte_count": None,
            "response_code_unit_count": None,
            "response_utf16_sha256": None,
            "failure_code": "host_llm_failed",
            "non_child_reason": None,
            "recovery_disposition": "resend_new_intake_after_provider_failure",
            "normalized_payload_hash": None,
            "source_field_state_hash": HASH,
            "ambiguity_hash": None,
            "evidence_set_hash": None,
        }
    )
    return material


def result_material_v2() -> dict[str, object]:
    material = result_material()
    material["schema_version"] = "finance-ai-result-material-v2"
    material["result_arguments_hash"] = HASH
    return material


def link_material() -> dict[str, object]:
    return {
        "schema_version": "finance-ai-link-material-v1",
        "result_public_id": "air_example",
        "result_material_hash": HASH,
        "proposal_public_id": "proposal_child",
        "effective_content_hash": HASH,
    }


def test_claim_material_matches_the_contract_sanity_vector() -> None:
    assert provenance.claim_material_hash(claim_material()) == (
        "a26fa04ffb4dac8bae33724c422e04ca8d7c239ea6325daa26ce9dbf5c0aafb5"
    )


def test_material_hashes_are_canonical_and_durable_ids_are_domain_separated() -> None:
    preparation = preparation_material()
    first = provenance.preparation_material_hash(preparation)
    assert first == "3edebfadb5f261b69db11330edcee5ecd044af21146de340042ab9815be87c4b"
    reordered = dict(reversed(tuple(preparation.items())))
    assert provenance.preparation_material_hash(reordered) == first

    attempt = provenance.derive_attempt_public_id(first)
    claim = provenance.derive_claim_public_id(attempt)
    result = provenance.derive_result_public_id(attempt)
    link = provenance.derive_link_public_id(result, "proposal_child")
    assert attempt.startswith("aifa_") and len(attempt) == 69
    assert claim.startswith("aicl_") and len(claim) == 69
    assert result.startswith("air_") and len(result) == 68
    assert link.startswith("aipl_") and len(link) == 69
    assert claim != result
    assert attempt == "aifa_31e7d81314eefe99adb0d1f5e726c3f64211c2255d48c97c1512c93dae57ba10"
    assert claim == "aicl_67d3a9a1b25928919c79abe45d41dd501e4a66b04a0d36eabbbf5aabd6e1faa8"
    assert result == "air_cbfd2ee0f3270b1be058928c4264f02659a019f5b060057d8ba1c8ae307016b9"
    assert link == "aipl_a0776e2ea542fc81e9016d90e8634f8e66d8a2b6a69519d613f061bca3b8fcec"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.__setitem__("unknown", "x"), "fields differ"),
        (
            lambda value: value.__setitem__(
                "eligibility_reasons",
                ["unsupported_language", "deterministic_fields_incomplete"],
            ),
            "sorted",
        ),
        (lambda value: value.__setitem__("eligibility_reasons", ["unknown_reason"]), "invalid"),
        (lambda value: value.__setitem__("source_kind", "receipt_bytes"), "source_kind"),
        (lambda value: value.__setitem__("request_byte_count", 65_537), "maximum"),
        (lambda value: value.__setitem__("runtime_policy_hash", "BAD"), "SHA-256"),
    ],
)
def test_preparation_material_refuses_unknown_or_noncanonical_values(mutator, message: str) -> None:
    material = preparation_material()
    mutator(material)
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match=message):
        provenance.preparation_material_hash(material)


def test_preparation_material_requires_mode_and_closed_reason_to_agree() -> None:
    material = preparation_material()
    material["eligibility_mode"] = "classification_only"
    material["eligibility_reasons"] = ["intent_classification_required"]
    assert len(provenance.preparation_material_hash(material)) == 64

    material["eligibility_reasons"] = ["unsupported_language"]
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="must agree"):
        provenance.preparation_material_hash(material)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda value: value.__setitem__("call_start_not_after_ms", 1_700_000_000_000),
            "must be after",
        ),
        (lambda value: value.__setitem__("unknown", "x"), "fields differ"),
    ],
)
def test_claim_material_refuses_unknown_fields_and_impossible_clocks(mutator, message: str) -> None:
    material = claim_material()
    mutator(material)
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match=message):
        provenance.claim_material_hash(material)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.__setitem__("proposal_public_id", None), "must not be null"),
        (lambda value: value.__setitem__("unknown", "x"), "fields differ"),
    ],
)
def test_link_material_refuses_unknown_or_missing_values(mutator, message: str) -> None:
    material = link_material()
    mutator(material)
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match=message):
        provenance.link_material_hash(material)


def test_result_material_enforces_closed_enums_and_recovery_exclusivity() -> None:
    material = result_material()
    assert len(provenance.result_material_hash(material)) == 64

    invalid_status = deepcopy(material)
    invalid_status["result_status"] = "retrying"
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="result_status"):
        provenance.result_material_hash(invalid_status)

    invalid_recovery = deepcopy(material)
    invalid_recovery["non_child_reason"] = "validation_refused"
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="cannot carry"):
        provenance.result_material_hash(invalid_recovery)

    invalid_count = deepcopy(material)
    invalid_count["response_byte_count"] = -1
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="response_byte_count"):
        provenance.result_material_hash(invalid_count)

    invalid_disposition = deepcopy(material)
    invalid_disposition["recovery_disposition"] = "retry"
    with pytest.raises(
        provenance.AiFallbackProvenanceValidationError,
        match="recovery_disposition",
    ):
        provenance.result_material_hash(invalid_disposition)

    impossible_post_lock = deepcopy(material)
    impossible_post_lock["post_lock_at_ms"] = 0
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="post_lock"):
        provenance.result_material_hash(impossible_post_lock)

    impossible_decision = deepcopy(material)
    impossible_decision["decision_at_ms"] = 1
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="decision_at_ms"):
        provenance.result_material_hash(impossible_decision)


def test_result_material_v2_binds_arguments_hash_without_rewriting_v1() -> None:
    legacy = result_material()
    assert provenance.result_material_hash(legacy) == (
        "5aaac259752aae94733b1369748b4d611baf890a5f425254f23edfe75f2c9847"
    )

    sealed = result_material_v2()
    first = provenance.result_material_v2_hash(sealed)
    assert first == "ad20d5f168fc9308324c502693d6c274385eed327153e1682e61d9e228051b37"
    changed = deepcopy(sealed)
    changed["result_arguments_hash"] = "1" * 64
    assert provenance.result_material_v2_hash(changed) != first

    missing = deepcopy(sealed)
    del missing["result_arguments_hash"]
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="fields differ"):
        provenance.result_material_v2_hash(missing)
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="fields differ"):
        provenance.result_material_v2_hash(legacy)


def test_result_material_enforces_transport_retention_and_failure_unions() -> None:
    received = result_material()
    received.update(
        {
            "transport_outcome": "response_received",
            "result_status": "proposal_created",
            "retention_state": "blob_retained",
            "response_body_state": "retained",
            "normal_attribution_hash": HASH,
            "usage_hash": HASH,
            "response_sha256": HASH,
            "response_byte_count": 2,
            "failure_code": None,
            "recovery_disposition": None,
        }
    )
    assert len(provenance.result_material_hash(received)) == 64

    retained_oversize = deepcopy(received)
    retained_oversize["result_status"] = "response_oversize"
    retained_oversize["recovery_disposition"] = "use_manual_intake"
    retained_oversize["response_byte_count"] = 16_385
    assert len(provenance.result_material_hash(retained_oversize)) == 64

    late_oversize = deepcopy(retained_oversize)
    late_oversize["result_status"] = "late_result"
    late_oversize["recovery_disposition"] = "resend_new_intake_after_late_result"
    assert len(provenance.result_material_hash(late_oversize)) == 64

    for status, recovery_disposition in (
        ("attribution_refused", "operator_runtime_review"),
        ("stale_parent", "review_current_parent_state"),
    ):
        pre_size_override = deepcopy(retained_oversize)
        pre_size_override["result_status"] = status
        pre_size_override["recovery_disposition"] = recovery_disposition
        assert len(provenance.result_material_hash(pre_size_override)) == 64

    for transport, retention_state, response_body_state, count_fields, status, recovery in (
        (
            "response_unencodable",
            "unretained_unencodable",
            "unencodable",
            {"response_code_unit_count": 1, "response_utf16_sha256": HASH},
            "attribution_refused",
            "operator_runtime_review",
        ),
        (
            "response_resource_refused",
            "unretained_resource_refused",
            "resource_refused",
            {"response_code_unit_count": 131_073},
            "stale_parent",
            "review_current_parent_state",
        ),
    ):
        pre_size_override = result_material()
        pre_size_override.update(
            {
                "transport_outcome": transport,
                "result_status": status,
                "retention_state": retention_state,
                "response_body_state": response_body_state,
                "normal_attribution_hash": HASH,
                "usage_hash": HASH,
                "failure_code": None,
                "recovery_disposition": recovery,
                **count_fields,
            }
        )
        assert len(provenance.result_material_hash(pre_size_override)) == 64

    late_provider_error = result_material()
    late_provider_error["result_status"] = "late_result"
    late_provider_error["recovery_disposition"] = "resend_new_intake_after_late_result"
    assert len(provenance.result_material_hash(late_provider_error)) == 64

    premature_oversize = deepcopy(retained_oversize)
    premature_oversize["response_byte_count"] = 16_384
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="16,384"):
        provenance.result_material_hash(premature_oversize)

    too_large_child = deepcopy(received)
    too_large_child["response_byte_count"] = 16_385
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="16,384"):
        provenance.result_material_hash(too_large_child)

    for status in ("classification_only", "response_refused"):
        too_large_non_child = deepcopy(received)
        too_large_non_child["result_status"] = status
        too_large_non_child["response_byte_count"] = 16_385
        with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="16,384"):
            provenance.result_material_hash(too_large_non_child)

    mismatched_status = deepcopy(received)
    mismatched_status["result_status"] = "provider_error"
    mismatched_status["recovery_disposition"] = "resend_new_intake_after_provider_failure"
    with pytest.raises(
        provenance.AiFallbackProvenanceValidationError,
        match="invalid result_status",
    ):
        provenance.result_material_hash(mismatched_status)

    mismatched_retention = deepcopy(received)
    mismatched_retention["retention_state"] = "none"
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="retention_state"):
        provenance.result_material_hash(mismatched_retention)

    preinvocation = result_material()
    preinvocation.update(
        {
            "transport_outcome": "local_preinvocation_refused",
            "result_status": "preinvocation_refused",
            "failure_code": "call_start_deadline_exceeded",
            "recovery_disposition": "operator_runtime_review",
        }
    )
    with pytest.raises(
        provenance.AiFallbackProvenanceValidationError,
        match="does not match failure_code",
    ):
        provenance.result_material_hash(preinvocation)


def test_canonical_request_hash_is_bounded_and_has_no_side_effectful_dependency() -> None:
    assert provenance.canonical_request_sha256(b'{"model":"synthetic/test"}') == (
        "aa47ef96bd985b86e015e38e75ddde8547817879801f44c35b50b985057f7a07"
    )
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="65,536"):
        provenance.canonical_request_sha256(b"x" * 65_537)

    assert provenance.canonical_response_sha256(b'{"proposal":"synthetic"}') == (
        "12b79e72eb3360f991ecdc917a37cc75321f06c1219942b944e27e219574cdd6"
    )
    with pytest.raises(provenance.AiFallbackProvenanceValidationError, match="response BLOB"):
        provenance.canonical_response_sha256(b"x" * 65_537)


def test_every_material_hash_has_a_synthetic_golden_vector_and_no_side_effect_import() -> None:
    assert provenance.result_material_hash(result_material()) == (
        "5aaac259752aae94733b1369748b4d611baf890a5f425254f23edfe75f2c9847"
    )
    assert provenance.link_material_hash(link_material()) == (
        "283a070ce5c0fd6388dc522d33968d280f7e0d8d1399860c7c73dae8ffa8d3c8"
    )

    tree = ast.parse(inspect.getsource(provenance))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported_roots <= {"__future__", "collections", "hashlib", "json", "re", "typing"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"__import__", "eval", "exec", "open"}
        for node in ast.walk(tree)
    )
