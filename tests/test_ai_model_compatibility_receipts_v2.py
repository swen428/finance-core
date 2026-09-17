"""Nomi v2 compatibility receipt and unreachable fallback boundary tests."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as bridge_support
import pytest

import finance_core.parser_proposals.ai_fallback as ai_fallback_module
import finance_core.parser_proposals.ai_model_admission as ai_model_admission_module
import finance_core.parser_proposals.ai_model_compatibility as ai_model_compatibility_module
from finance_core.openclaw_staging_bridge import commands as bridge_commands
from finance_core.openclaw_staging_bridge import envelope as bridge_envelope
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    claim_ai_fallback_invocation,
    claim_ai_fallback_invocation_v2,
    get_ai_processing_status_v2,
    prepare_ai_fallback,
    prepare_ai_fallback_v2,
    record_ai_fallback_result_v2,
    record_ai_fallback_result_with_disposition,
)
from finance_core.parser_proposals.ai_model_admission import (
    AiModelAdmissionError,
    _record_ai_model_admission_denial_v2,
)
from finance_core.parser_proposals.ai_model_compatibility import (
    ModelCompatibilityError,
    canonical_projection_hash,
    link_attempt_to_receipt,
    register_ai_model_compatibility_receipt_v2,
    resolve_compatibility_receipt,
    validate_config_projection,
    verify_persisted_compatibility_receipt,
)
from finance_core.parser_proposals.ai_processing_status_v2 import (
    PROCESSING_PATHS,
    derive_ai_processing_status_v2,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_ai_fallback_service_v1 import (
    _captured_text_proposal,
    _response_body,
    _set_parent_payload_fields,
)


def _projection() -> dict[str, Any]:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/finance_ai/agent_projection_v2_golden.json").read_text(
            encoding="utf-8"
        )
    )
    return fixture["projection"]


def _response(case: dict[str, Any]) -> bytes:
    expected = case["expected"]
    refs = {
        "clear_text": {"merchant": "t0001", "amount": "t0001", "currency": "t0001"},
        "clear_ocr": {"merchant": "e0001", "amount": "e0003", "currency": "e0003"},
        "ambiguous_amount_currency": {"merchant": "e0001"},
        "ocr_prompt_injection": {"merchant": "e0001", "amount": "e0002", "currency": "e0002"},
    }[case["case_id"]]
    values = {
        "amount": expected["amount"],
        "currency": expected["currency"],
        "transaction_date": expected["transaction_date"],
        "merchant": expected["merchant"],
        "description": expected["description"],
        "account": expected["account"],
        "category": expected["category"],
    }
    response = {
        "schema_version": "finance-ai-facts-v2",
        "intent_type": expected["intent_type"],
        **values,
        "field_confidence_bps": {
            field: (9000 if value is not None else None) for field, value in values.items()
        },
        "field_conflicts": {
            field: [] for field in ("amount", "currency", "transaction_date", "merchant")
        },
        "field_evidence_refs": {
            field: ([refs[field]] if value is not None else []) for field, value in values.items()
        },
    }
    return json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_cases() -> list[dict[str, Any]]:
    return json.loads(
        (
            Path(__file__).parents[1]
            / "finance_core/resources/finance_ai/model_compatibility_fixture_set_v1.json"
        ).read_text(encoding="utf-8")
    )["cases"]


def test_compatibility_fixtures_match_missing_field_ambiguity_contract() -> None:
    for case in _fixture_cases():
        expected = case["expected"]
        flags = expected["ambiguity_flags"]
        assert flags == sorted(set(flags))
        assert ("missing_date" in flags) is (expected["transaction_date"] is None)


def test_compatibility_assets_use_new_immutable_versions() -> None:
    registry = json.loads(
        (
            Path(__file__).parents[1] / "finance_core/resources/finance_ai/asset_registry_v2.json"
        ).read_text(encoding="utf-8")
    )
    assert registry["assets"]["prompt"]["version"] == "finance-ai-prompt-v5"
    assert (
        registry["assets"]["model_compatibility_fixtures"]["version"]
        == "finance-ai-model-compatibility-fixtures-v6"
    )


def test_prompt_requests_facts_without_internal_vocabulary() -> None:
    prompt = (
        Path(__file__).parents[1] / "finance_core/resources/finance_ai/prompt_v1.txt"
    ).read_text()
    assert '"schema_version": "finance-ai-facts-v2"' in prompt
    assert '"field_conflicts"' in prompt
    assert '"ambiguity_flags":' not in prompt
    assert "missing_date" not in prompt


def _outcomes() -> list[dict[str, Any]]:
    results = []
    for case in _fixture_cases():
        results.append(
            {
                "case_id": case["case_id"],
                "ordinary_agent_turn_count": 0,
                "isolated_completion_count": 1,
                "provider_dispatch_count": 1,
                "effective_max_retries": 0,
                "elapsed_ms": 100,
                "observed_provider": "openai",
                "observed_model": "example-model",
                "observed_agent_id": "finance",
                "response_utf8_b64": base64.b64encode(_response(case)).decode("ascii"),
            }
        )
    return results


def _with_response(outcome: dict[str, Any], response: bytes) -> dict[str, Any]:
    mutated = dict(outcome)
    mutated["response_utf8_b64"] = base64.b64encode(response).decode("ascii")
    return mutated


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    return conn


def _register(conn: sqlite3.Connection) -> dict[str, Any]:
    return register_ai_model_compatibility_receipt_v2(
        conn,
        config_projection=_projection(),
        harness_outcomes=_outcomes(),
        now_ms=1000,
    )


def _prepared_v2_connection(tmp_path: Path) -> tuple[sqlite3.Connection, str, dict[str, Any]]:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    parent = conn.execute(
        "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert parent is not None
    for column in ("parsed_payload", "normalized_payload"):
        payload = json.loads(parent[column])
        payload.update({"description": None, "account": None, "category": None})
        conn.execute(
            f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), parent_id),
        )
    conn.commit()
    receipt = _register(conn)
    prepared = prepare_ai_fallback_v2(
        conn,
        intake_public_id=intake_public_id,
        config_projection=_projection(),
        now_ms=100_000,
    )
    receipt_id = conn.execute(
        "SELECT id FROM ai_model_compatibility_receipts WHERE receipt_public_id = ?",
        (receipt["receipt_public_id"],),
    ).fetchone()[0]
    return conn, intake_public_id, {**prepared, "receipt_id": receipt_id}


def _eligible_connection_without_receipt(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    parent = conn.execute(
        "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert parent is not None
    for column in ("parsed_payload", "normalized_payload"):
        payload = json.loads(parent[column])
        payload.update({"description": None, "account": None, "category": None})
        conn.execute(
            f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), parent_id),
        )
    conn.commit()
    return conn, intake_public_id


def test_missing_receipt_persists_terminal_admission_denial(tmp_path: Path) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    try:
        with pytest.raises(AiFallbackServiceError, match="no unique accepted receipt"):
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        decision = conn.execute("SELECT * FROM ai_model_admission_decisions").fetchone()
        assert decision is not None
        assert decision["decision_type"] == "model_denied"
        assert decision["safe_reason_code"] == "configuration_not_accepted"
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "model_denied"
        assert status["safe_reason_code"] == "configuration_not_accepted"
        assert status["attempt_public_id"] is None
        assert status["admission_decision_public_id"] == decision["decision_public_id"]
        with pytest.raises(AiFallbackServiceError, match="terminal model-admission denial"):
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_001,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_deterministic_complete_refuses_before_receipt_and_writes_no_denial(
    tmp_path: Path,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(
        tmp_path,
        "personal expense paid SGD 12.34 at Cafe on 2026-08-13",
    )
    _set_parent_payload_fields(
        conn,
        parent_id,
        {
            "amount": "12.34",
            "currency": "SGD",
            "merchant": "Cafe",
            "transaction_date": "2026-08-13",
            "transaction_type": "personal_expense",
            "description": None,
            "account": None,
            "category": None,
        },
    )
    try:
        with pytest.raises(AiFallbackServiceError) as exc_info:
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert exc_info.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert exc_info.value.details == {
            "eligibility_disposition": "review_existing",
            "refusal_reason": "deterministic_complete",
        }
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "no_model"
        assert status["safe_reason_code"] is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "text,parent_updates,expected_reason",
    [
        ("paid by Alice SGD 12.34 at Cafe", {}, "explicit_deny"),
        (
            "paid SGD 12.34 at Cafe sk-proj-abcdefghijklmnopqrstuvwxyz",
            {},
            "sensitive_text_refused",
        ),
        ("paid SGD 12.34 at Cafe", {"account": "checking"}, "forbidden_field"),
    ],
)
def test_non_model_eligibility_refuses_before_receipt_and_writes_no_denial(
    tmp_path: Path,
    text: str,
    parent_updates: dict[str, object],
    expected_reason: str,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path, text)
    if parent_updates:
        _set_parent_payload_fields(conn, parent_id, parent_updates)
    try:
        with pytest.raises(AiFallbackServiceError) as exc_info:
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert exc_info.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert exc_info.value.details is not None
        assert exc_info.value.details["refusal_reason"] == expected_reason
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_missing_receipt_rechecks_eligibility_before_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    parent_id = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
        (intake_public_id,),
    ).fetchone()[0]

    def complete_parent_then_refuse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        _set_parent_payload_fields(
            conn,
            parent_id,
            {
                "amount": "12.34",
                "currency": "SGD",
                "merchant": "Cafe",
                "transaction_date": "2026-08-13",
                "transaction_type": "personal_expense",
                "description": None,
                "account": None,
                "category": None,
            },
        )
        raise ModelCompatibilityError("AI_MODEL_CONFIG_NOT_ACCEPTED", "missing")

    monkeypatch.setattr(
        ai_fallback_module,
        "resolve_compatibility_receipt",
        complete_parent_then_refuse,
    )
    try:
        with pytest.raises(AiFallbackServiceError) as exc_info:
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert exc_info.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert exc_info.value.details is not None
        assert exc_info.value.details["refusal_reason"] == "deterministic_complete"
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_receipt_registered_after_initial_lookup_prevents_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    original_resolve = ai_model_compatibility_module.resolve_compatibility_receipt
    lookup_count = 0

    def register_then_refuse(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            _register(conn)
            raise ModelCompatibilityError("AI_MODEL_CONFIG_NOT_ACCEPTED", "missing")
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        ai_fallback_module,
        "resolve_compatibility_receipt",
        register_then_refuse,
    )
    try:
        prepared = prepare_ai_fallback_v2(
            conn,
            intake_public_id=intake_public_id,
            config_projection=_projection(),
            now_ms=100_000,
        )
        assert prepared["claim_disposition"] == "claim_once"
        assert lookup_count == 2
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_v2_prepare_replays_existing_receipt_bound_attempt(tmp_path: Path) -> None:
    conn, intake_public_id, first = _prepared_v2_connection(tmp_path)
    try:
        replay = prepare_ai_fallback_v2(
            conn,
            intake_public_id=intake_public_id,
            config_projection=_projection(),
            now_ms=100_001,
        )
        assert replay["attempt_public_id"] == first["attempt_public_id"]
        assert replay["receipt_public_id"] == first["receipt_public_id"]
        assert replay["claim_disposition"] == "do_not_claim"
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_admission_denial_writer_is_internal_only() -> None:
    assert not hasattr(ai_model_admission_module, "record_ai_model_admission_denial_v2")
    assert "record_ai_model_admission_denial_v2" not in ai_model_admission_module.__all__


def test_admission_denial_is_append_only_and_excludes_attempt(tmp_path: Path) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    try:
        _record_ai_model_admission_denial_v2(
            conn,
            intake_public_id=intake_public_id,
            config_evidence=_projection(),
            now_ms=100_000,
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ai_model_admission_decisions SET decided_at_ms = decided_at_ms")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ai_model_admission_decisions")
        conn.rollback()
        _register(conn)
        with pytest.raises(AiFallbackServiceError, match="terminal model-admission denial"):
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_001,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
    finally:
        conn.close()


def test_admission_denial_refuses_foreign_keys_off_without_writes(tmp_path: Path) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(AiModelAdmissionError, match="Foreign keys"):
            _record_ai_model_admission_denial_v2(
                conn,
                intake_public_id=intake_public_id,
                config_evidence=_projection(),
                now_ms=100_000,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_admission_denial_concurrent_replay_is_one_row(tmp_path: Path) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)
    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    def worker() -> tuple[str, bool]:
        worker_conn = sqlite3.connect(database_path, timeout=5)
        worker_conn.row_factory = sqlite3.Row
        worker_conn.execute("PRAGMA foreign_keys = ON")
        try:
            decision, replay = _record_ai_model_admission_denial_v2(
                worker_conn,
                intake_public_id=intake_public_id,
                config_evidence=_projection(),
                now_ms=100_000,
            )
            return decision["decision_public_id"], replay
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: worker(), range(2)))
    assert len({public_id for public_id, _replay in results}) == 1
    assert sorted(replay for _public_id, replay in results) == [False, True]

    verified = sqlite3.connect(database_path)
    try:
        assert (
            verified.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 1
        )
        assert verified.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
    finally:
        verified.close()


def test_admission_denial_verification_failure_rolls_back_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)

    def refuse_verification(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AiModelAdmissionError(
            "AI_MODEL_COMPATIBILITY_CONFLICT", "injected verification refusal"
        )

    monkeypatch.setattr(
        ai_model_admission_module,
        "verify_ai_model_admission_decision",
        refuse_verification,
    )
    try:
        with pytest.raises(AiModelAdmissionError, match="injected verification refusal"):
            _record_ai_model_admission_denial_v2(
                conn,
                intake_public_id=intake_public_id,
                config_evidence=_projection(),
                now_ms=100_000,
            )
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "error_code",
    [
        "AI_MODEL_COMPATIBILITY_CONFLICT",
        "AI_MODEL_COMPATIBILITY_POLICY_REFUSED",
    ],
)
def test_integrity_or_policy_receipt_failure_never_persists_config_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    conn, intake_public_id = _eligible_connection_without_receipt(tmp_path)

    def refuse_resolution(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ModelCompatibilityError(error_code, "injected compatibility refusal")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                ai_fallback_module,
                "resolve_compatibility_receipt",
                refuse_resolution,
            )
            with pytest.raises(AiFallbackServiceError) as exc_info:
                prepare_ai_fallback_v2(
                    conn,
                    intake_public_id=intake_public_id,
                    config_projection=_projection(),
                    now_ms=100_000,
                )
            assert exc_info.value.code == error_code
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0

        _register(conn)
        prepared = prepare_ai_fallback_v2(
            conn,
            intake_public_id=intake_public_id,
            config_projection=_projection(),
            now_ms=100_001,
        )
        assert prepared["claim_disposition"] == "claim_once"
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 1
    finally:
        conn.close()


def test_python_projection_authority_matches_shared_golden() -> None:
    golden = json.loads(
        (Path(__file__).parent / "fixtures/finance_ai/agent_projection_v2_golden.json").read_text(
            encoding="utf-8"
        )
    )
    projection = golden["projection"]
    assert golden["source"]["openclawVersion"] == "2026.7.1"
    assert projection["openclaw_version"] == golden["source"]["openclawVersion"]
    assert projection["openclaw_version"] != "2026.7.1-2"
    assert validate_config_projection(projection) == projection
    assert canonical_projection_hash(projection) == golden["projection_sha256"]


def test_model_compatibility_error_exit_classes_are_stable() -> None:
    assert (
        bridge_commands._map_model_compatibility_error(
            ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", "invalid")
        ).exit_code
        == bridge_errors.EXIT_VALIDATION_REFUSED
    )
    assert (
        bridge_commands._map_model_compatibility_error(
            ModelCompatibilityError("AI_MODEL_CONFIG_NOT_ACCEPTED", "missing")
        ).exit_code
        == bridge_errors.EXIT_AUTHORITY_REFUSED
    )
    assert (
        bridge_commands._map_model_compatibility_error(
            ModelCompatibilityError("AI_MODEL_COMPATIBILITY_INTERNAL", "failed")
        ).exit_code
        == bridge_errors.EXIT_INTERNAL
    )


@pytest.mark.parametrize(
    "case_id,expected_response_sha256",
    [
        ("clear_text", "e1ead760479135f74a3919cb4a4c7313acfb1ff9a15670cef1fb81de129c6c99"),
        ("clear_ocr", "d843a74dd570e440770283586952e1a40009c155820e766d695a3736fdf1b14a"),
        (
            "ambiguous_amount_currency",
            "606b2a50553f833ce74adcc2819a9a622043b520abbec70760bf47c005f819c9",
        ),
        (
            "ocr_prompt_injection",
            "5437b789cf741aef94d6bfad61ebfb920869e441db636ad2255a56f31f95076c",
        ),
    ],
)
def test_single_harness_outcome_verifies_each_fixed_case(
    case_id: str,
    expected_response_sha256: str,
) -> None:
    outcome = next(item for item in _outcomes() if item["case_id"] == case_id)
    verified = ai_model_compatibility_module.verify_harness_outcome(_projection(), outcome)
    assert verified["case_id"] == case_id
    assert verified["response_sha256"] == expected_response_sha256
    assert (
        verified["response_sha256"]
        == hashlib.sha256(base64.b64decode(outcome["response_utf8_b64"], validate=True)).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation,expected_reason,expected_field",
    [
        ("malformed_json", "RESPONSE_JSON_INVALID", None),
        ("malformed_base64", "RESPONSE_ENCODING_INVALID", None),
        ("wrong_amount", "FIELD_MISMATCH", "amount"),
        ("wrong_evidence_reference", "EVIDENCE_REFERENCE_INVALID", "amount"),
        ("forbidden_injection", "PROMPT_INJECTION_ECHO", None),
        ("wrong_case_id", "CASE_IDENTITY_INVALID", None),
        ("flags_not_list", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_unknown", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_duplicate", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_unsorted", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_missing", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_extra", "RESPONSE_SCHEMA_INVALID", None),
        ("flags_mismatch", "RESPONSE_SCHEMA_INVALID", None),
    ],
)
def test_single_harness_outcome_refuses_invalid_case_evidence(
    mutation: str,
    expected_reason: str,
    expected_field: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcomes()[0]
    if mutation == "malformed_json":
        outcome["response_utf8_b64"] = base64.b64encode(b"{").decode("ascii")
    elif mutation == "malformed_base64":
        outcome["response_utf8_b64"] = "not-base64"
    elif mutation in {"wrong_amount", "wrong_evidence_reference"}:
        decoded = json.loads(base64.b64decode(outcome["response_utf8_b64"]))
        if mutation == "wrong_amount":
            decoded["amount"] = "99.99"
        else:
            decoded["field_evidence_refs"]["amount"] = ["x9999"]
        outcome = _with_response(
            outcome,
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
    elif mutation == "forbidden_injection":
        assets = ai_model_compatibility_module.policy_assets()
        assets["fixtures"]["cases"][0]["forbidden_output_fragments"] = ["finance-ai-facts-v2"]
        monkeypatch.setattr(
            ai_model_compatibility_module,
            "policy_assets",
            lambda *_args, **_kwargs: assets,
        )
    elif mutation == "wrong_case_id":
        outcome["case_id"] = "unknown_case"
    else:
        decoded = json.loads(base64.b64decode(outcome["response_utf8_b64"]))
        decoded["ambiguity_flags"] = {
            "flags_not_list": None,
            "flags_unknown": ["unknown_flag"],
            "flags_duplicate": ["missing_date", "missing_date"],
            "flags_unsorted": ["missing_date", "ambiguous_amount"],
            "flags_missing": [],
            "flags_extra": ["ambiguous_amount", "missing_date"],
            "flags_mismatch": ["ambiguous_amount"],
        }[mutation]
        outcome = _with_response(
            outcome,
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    with pytest.raises(ModelCompatibilityError) as exc_info:
        ai_model_compatibility_module.verify_harness_outcome(_projection(), outcome)
    assert exc_info.value.code == "AI_MODEL_EVAL_REFUSED"
    expected_details = {"verification_reason": expected_reason}
    if expected_field is not None:
        expected_details["verification_field"] = expected_field
    assert exc_info.value.details == expected_details


def test_single_case_bridge_command_is_read_only_without_workspace_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = bridge_envelope.COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2
    assert command in bridge_envelope.ALLOWED_COMMANDS
    assert command not in bridge_envelope.MUTATING_COMMANDS

    def fail_open_context(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("single-case verification must not open a workspace database")

    monkeypatch.setattr(bridge_commands, "_open_context", fail_open_context)
    result = bridge_support.run_cli(
        bridge_support.make_request(
            command,
            {
                "config_projection": _projection(),
                "harness_outcome": _outcomes()[0],
            },
        )
    )
    assert result.exit_code == bridge_errors.EXIT_OK
    assert result.response["status"] == "ok"
    assert result.response["result"] == {
        "case_id": "clear_text",
        "verified": True,
        "response_sha256": hashlib.sha256(_response(_fixture_cases()[0])).hexdigest(),
    }
    assert list(tmp_path.iterdir()) == []


def test_single_case_bridge_command_refuses_invalid_evidence_without_workspace_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = bridge_envelope.COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2

    def fail_open_context(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("single-case verification must not open a workspace database")

    monkeypatch.setattr(bridge_commands, "_open_context", fail_open_context)
    outcome = _outcomes()[0]
    outcome["response_utf8_b64"] = "not-base64"
    result = bridge_support.run_cli(
        bridge_support.make_request(
            command,
            {
                "config_projection": _projection(),
                "harness_outcome": outcome,
            },
        )
    )
    assert result.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
    assert result.response["status"] == "error"
    assert result.response["error"]["code"] == "AI_MODEL_EVAL_REFUSED"
    assert result.response["error"]["details"] == {
        "verification_reason": "RESPONSE_ENCODING_INVALID"
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "claim_exists,result_status,failure_code,execution_class,expected_path,expected_reason",
    [
        (False, None, None, "cloud_projection", "no_model", None),
        (True, None, None, "cloud_projection", "outcome_unknown", "outcome_unknown"),
        (True, "proposal_created", None, "cloud_projection", "cloud_projection", None),
        (True, "classification_only", None, "local_model", "local_model", None),
        (
            True,
            "preinvocation_refused",
            "request_integrity_refused",
            "cloud_projection",
            "model_denied",
            "configuration_not_accepted",
        ),
        (
            True,
            "preinvocation_refused",
            "runtime_policy_refused",
            "cloud_projection",
            "model_denied",
            "policy_denied",
        ),
        (
            True,
            "preinvocation_refused",
            "call_start_deadline_exceeded",
            "cloud_projection",
            "model_denied",
            "deadline_exceeded",
        ),
        (
            True,
            "stale_parent",
            None,
            "cloud_projection",
            "model_failed",
            "status_unavailable",
        ),
        (True, "timeout", "timeout", "cloud_projection", "model_failed", "deadline_exceeded"),
        (True, "late_result", None, "cloud_projection", "model_failed", "deadline_exceeded"),
        (
            True,
            "provider_error",
            "provider_error",
            "cloud_projection",
            "model_failed",
            "provider_unavailable",
        ),
        (True, "response_refused", None, "cloud_projection", "model_failed", "output_invalid"),
        (True, "response_oversize", None, "cloud_projection", "model_failed", "output_invalid"),
        (True, "response_unencodable", None, "cloud_projection", "model_failed", "output_invalid"),
        (
            True,
            "response_resource_refused",
            None,
            "cloud_projection",
            "model_failed",
            "output_invalid",
        ),
        (True, "cancelled", "cancelled", "cloud_projection", "model_failed", "outcome_unknown"),
        (
            True,
            "attribution_refused",
            None,
            "cloud_projection",
            "model_failed",
            "attribution_mismatch",
        ),
    ],
)
def test_processing_status_projection_uses_closed_paths(
    claim_exists: bool,
    result_status: str | None,
    failure_code: str | None,
    execution_class: str,
    expected_path: str,
    expected_reason: str | None,
) -> None:
    status = derive_ai_processing_status_v2(
        {
            "intake_public_id": "ocri_example",
            "attempt_public_id": "aifa_example",
            "receipt_public_id": "aimr_example",
            "canonical_provider": "openai",
            "canonical_model": "example-model",
            "agent_id": "finance",
            "display_alias": "Nomi 云端 🚀",
            "execution_class": execution_class,
        },
        claim_exists=claim_exists,
        result_status=result_status,
        failure_code=failure_code,
    )
    assert status["processing_path"] == expected_path
    assert status["processing_path"] in PROCESSING_PATHS
    assert status["safe_reason_code"] == expected_reason


def test_processing_status_handles_absent_or_unavailable_durable_state() -> None:
    no_attempt = derive_ai_processing_status_v2(
        {"intake_public_id": "ocri_example", "attempt_public_id": None},
        claim_exists=False,
        result_status=None,
        failure_code=None,
    )
    assert no_attempt["processing_path"] == "no_model"
    no_receipt = derive_ai_processing_status_v2(
        {
            "intake_public_id": "ocri_example",
            "attempt_public_id": "aifa_v1",
            "receipt_public_id": None,
        },
        claim_exists=True,
        result_status=None,
        failure_code=None,
    )
    assert no_receipt["processing_path"] == "model_denied"
    assert no_receipt["safe_reason_code"] == "status_unavailable"


@pytest.mark.parametrize("field", ["prompt_version", "prompt_sha256"])
def test_projection_refuses_prompt_binding_drift(field: str) -> None:
    projection = _projection()
    projection[field] = "finance-ai-prompt-v999" if field == "prompt_version" else "9" * 64
    with pytest.raises(ModelCompatibilityError, match="frozen v2 policy"):
        validate_config_projection(projection)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("agent_id"),
        lambda value: value.__setitem__("unexpected", "value"),
        lambda value: value.__setitem__("effective_max_retries", False),
        lambda value: value.__setitem__("fallbacks", ["other/model"]),
        lambda value: value.__setitem__("display_alias", "名" * 22),
        lambda value: value.__setitem__("display_alias", "bad\ud800name"),
        lambda value: value.__setitem__("display_alias", "bad\u202ename"),
    ],
)
def test_python_projection_refuses_shape_and_policy_drift(mutation: Any) -> None:
    projection = _projection()
    mutation(projection)
    with pytest.raises(ModelCompatibilityError):
        validate_config_projection(projection)


def test_registration_recomputes_four_cases_and_replays_append_only() -> None:
    conn = _connection()
    try:
        first = _register(conn)
        second = _register(conn)
        assert first["receipt_public_id"] == second["receipt_public_id"]
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 1
        )
        summary = json.loads(
            conn.execute(
                "SELECT fixture_results_json FROM ai_model_compatibility_receipts"
            ).fetchone()[0]
        )
        assert set(summary[0]) == {
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
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ai_model_compatibility_receipts SET issued_at_ms = 2")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ai_model_compatibility_receipts")
        conn.rollback()
        changed = _outcomes()
        changed[0]["elapsed_ms"] = 101
        with pytest.raises(ModelCompatibilityError, match="different eval material"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=changed
            )
    finally:
        conn.close()


def test_exact_receipt_resolution_refuses_config_or_build_drift() -> None:
    conn = _connection()
    try:
        receipt = _register(conn)
        resolved = resolve_compatibility_receipt(conn, config_projection=_projection())
        assert resolved["receipt_public_id"] == receipt["receipt_public_id"]
        for field in ("plugin_build_sha256", "openclaw_package_sha256"):
            drifted = _projection()
            drifted[field] = "9" * 64
            with pytest.raises(ModelCompatibilityError, match="no unique accepted receipt"):
                resolve_compatibility_receipt(conn, config_projection=drifted)
    finally:
        conn.close()


def test_historical_receipt_verification_does_not_consult_current_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    try:
        receipt = _register(conn)
        row = conn.execute(
            "SELECT * FROM ai_model_compatibility_receipts WHERE receipt_public_id = ?",
            (receipt["receipt_public_id"],),
        ).fetchone()
        assert row is not None
        monkeypatch.setattr(
            "finance_core.parser_proposals.ai_model_compatibility.policy_assets",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not load assets")),
        )
        verified = verify_persisted_compatibility_receipt(dict(row))
        assert verified["canonical_model"] == "example-model"
    finally:
        conn.close()


def test_failed_eval_and_fk_off_create_no_receipt() -> None:
    conn = _connection()
    try:
        outcomes = _outcomes()
        outcomes[2]["provider_dispatch_count"] = 2
        with pytest.raises(ModelCompatibilityError, match="did not pass"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        outcomes = _outcomes()
        outcomes[0]["elapsed_ms"] = 30_000
        with pytest.raises(ModelCompatibilityError, match="did not pass"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(ModelCompatibilityError, match="Foreign keys"):
            _register(conn)
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_registration_persistence_failure_rolls_back_without_partial_receipt() -> None:
    conn = _connection()
    try:
        conn.execute(
            """
            CREATE TEMP TRIGGER fail_compatibility_receipt_insert
            BEFORE INSERT ON ai_model_compatibility_receipts
            BEGIN
                SELECT RAISE(ABORT, 'forced test-only persistence failure');
            END
            """
        )
        with pytest.raises(ModelCompatibilityError, match="Receipt persistence failed"):
            _register(conn)
        assert conn.in_transaction is False
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_eval_refuses_duplicate_evidence_refs_and_malformed_response() -> None:
    conn = _connection()
    try:
        outcomes = _outcomes()
        decoded = json.loads(base64.b64decode(outcomes[0]["response_utf8_b64"]))
        decoded["field_evidence_refs"]["amount"] *= 2
        outcomes[0]["response_utf8_b64"] = base64.b64encode(
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        with pytest.raises(ModelCompatibilityError, match="did not pass"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes
            )
        outcomes = _outcomes()
        outcomes[0]["response_utf8_b64"] = "not-base64"
        with pytest.raises(ModelCompatibilityError, match="did not pass"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_registration_refuses_fixture_case_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    try:
        assets = ai_model_compatibility_module.policy_assets()
        assets["fixtures"]["cases"][0]["case_id"] = "renamed_clear_text"
        monkeypatch.setattr(
            "finance_core.parser_proposals.ai_model_compatibility.policy_assets",
            lambda *_args, **_kwargs: assets,
        )
        with pytest.raises(ModelCompatibilityError, match="four-case"):
            register_ai_model_compatibility_receipt_v2(
                conn,
                config_projection=_projection(),
                harness_outcomes=_outcomes(),
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ordinary_agent_turn_count", False),
        ("isolated_completion_count", True),
        ("provider_dispatch_count", True),
        ("effective_max_retries", False),
    ],
)
def test_harness_count_fields_refuse_booleans(field: str, value: bool) -> None:
    conn = _connection()
    try:
        outcomes = _outcomes()
        outcomes[0][field] = value
        with pytest.raises(ModelCompatibilityError, match="did not pass"):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_migration_has_no_backfill_and_real_unique_link() -> None:
    conn = _connection()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_attempt_receipt_link_is_append_only_unique_and_fk_guarded(tmp_path: Path) -> None:
    conn, _intake_public_id, prepared = _prepared_v2_connection(tmp_path)
    try:
        attempt_id = conn.execute(
            "SELECT id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (prepared["attempt_public_id"],),
        ).fetchone()[0]
        binding = dict(
            conn.execute(
                "SELECT * FROM ai_fallback_attempt_compatibility_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE ai_fallback_attempt_compatibility_receipts SET created_at = created_at"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ai_fallback_attempt_compatibility_receipts")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                INSERT INTO ai_fallback_attempt_compatibility_receipts (
                    link_public_id, link_material_hash, attempt_id, receipt_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    binding["link_public_id"],
                    binding["link_material_hash"],
                    attempt_id,
                    prepared["receipt_id"],
                ),
            )
        conn.rollback()
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(ModelCompatibilityError, match="Foreign keys"):
            link_attempt_to_receipt(
                conn,
                attempt_id=attempt_id,
                receipt_id=prepared["receipt_id"],
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("expected_provider", "other-provider"),
        ("expected_model", "other-model"),
        ("expected_agent_id", "other-agent"),
        ("expected_audit_caller_kind", "operator"),
        ("expected_audit_caller_id", "other-caller"),
        ("expected_audit_caller_name", "unexpected-name"),
        ("expected_audit_purpose", "other-purpose"),
        ("expected_audit_session_key_sha256", "9" * 64),
        ("runtime_policy_version", "other-policy-version"),
        ("runtime_policy_hash", "9" * 64),
        ("prompt_version", "other-prompt-version"),
        ("prompt_template_hash", "9" * 64),
    ],
)
def test_attempt_receipt_link_refuses_every_attribution_mismatch(
    tmp_path: Path, column: str, value: str
) -> None:
    conn, _intake_public_id, prepared = _prepared_v2_connection(tmp_path)
    try:
        attempt_id = conn.execute(
            "SELECT id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (prepared["attempt_public_id"],),
        ).fetchone()[0]
        binding = dict(
            conn.execute(
                "SELECT * FROM ai_fallback_attempt_compatibility_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        )
        conn.executescript(
            """
            DROP TRIGGER trg_ai_fallback_attempt_receipts_no_delete;
            DROP TRIGGER trg_ai_fallback_attempts_no_update;
            DELETE FROM ai_fallback_attempt_compatibility_receipts;
            """
        )
        conn.execute(
            f"UPDATE ai_fallback_attempts SET {column} = ? WHERE id = ?", (value, attempt_id)
        )
        with pytest.raises(sqlite3.IntegrityError, match="attribution does not match"):
            conn.execute(
                """
                INSERT INTO ai_fallback_attempt_compatibility_receipts (
                    link_public_id, link_material_hash, attempt_id, receipt_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    binding["link_public_id"],
                    binding["link_material_hash"],
                    attempt_id,
                    prepared["receipt_id"],
                ),
            )
    finally:
        conn.close()


def test_corrupt_attempt_receipt_binding_fails_claim_and_closes_status(tmp_path: Path) -> None:
    conn, intake_public_id, prepared = _prepared_v2_connection(tmp_path)
    try:
        conn.execute("DROP TRIGGER trg_ai_fallback_attempt_receipts_no_update")
        conn.execute(
            "UPDATE ai_fallback_attempt_compatibility_receipts SET link_material_hash = ?",
            ("9" * 64,),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="binding does not verify"):
            claim_ai_fallback_invocation_v2(
                conn,
                attempt_public_id=prepared["attempt_public_id"],
                now_ms=100_001,
            )
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "model_denied"
        assert status["safe_reason_code"] == "status_unavailable"
        assert status["canonical_attribution"] is None
        assert status["display_alias"] is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "failure_code,expected_reason",
    [
        ("request_integrity_refused", "configuration_not_accepted"),
        ("runtime_policy_refused", "policy_denied"),
        ("call_start_deadline_exceeded", "deadline_exceeded"),
    ],
)
def test_persisted_preinvocation_failure_status_uses_failure_code(
    tmp_path: Path, failure_code: str, expected_reason: str
) -> None:
    conn, intake_public_id, prepared = _prepared_v2_connection(tmp_path)
    try:
        claim_ai_fallback_invocation_v2(
            conn,
            attempt_public_id=prepared["attempt_public_id"],
            now_ms=100_001,
        )
        result, replay = record_ai_fallback_result_v2(
            conn,
            attempt_public_id=prepared["attempt_public_id"],
            transport_outcome="local_preinvocation_refused",
            arguments={"failure_code": failure_code},
            now_ms=100_002,
        )
        assert replay is False
        assert result["result_status"] == "preinvocation_refused"
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "model_denied"
        assert status["safe_reason_code"] == expected_reason
    finally:
        conn.close()


def test_result_time_parent_drift_preserves_recovery_without_policy_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, intake_public_id, prepared = _prepared_v2_connection(tmp_path)
    try:
        claim = claim_ai_fallback_invocation_v2(
            conn,
            attempt_public_id=prepared["attempt_public_id"],
            now_ms=100_001,
        )
        _body, arguments = _response_body(claim)
        arguments.update(
            {
                "returned_model": "example-model",
                "returned_agent_id": "finance",
                "audit_purpose": "finance-bridge.ai-proposal-v2",
            }
        )
        verify_parent = ai_fallback_module._verify_attempt_current_parent_state

        def introduce_result_time_drift(
            connection: sqlite3.Connection, attempt: dict[str, Any]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            intake, parent = verify_parent(connection, attempt)
            connection.execute(
                "UPDATE parser_outputs SET parse_status = 'confirmed' WHERE id = ?",
                (attempt["parent_parser_output_id"],),
            )
            return intake, parent

        monkeypatch.setattr(
            ai_fallback_module,
            "_verify_attempt_current_parent_state",
            introduce_result_time_drift,
        )
        result, replay = record_ai_fallback_result_v2(
            conn,
            attempt_public_id=prepared["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert replay is False
        assert result["result_status"] == "stale_parent"
        assert result["recovery_disposition"] == "review_current_parent_state"
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "model_failed"
        assert status["safe_reason_code"] == "status_unavailable"
        assert status["safe_reason_code"] != "policy_denied"
    finally:
        conn.close()


def test_concurrent_registration_and_claim_are_one_shot(tmp_path: Path) -> None:
    workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    try:
        parent = conn.execute(
            "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
            (parent_id,),
        ).fetchone()
        assert parent is not None
        for column in ("parsed_payload", "normalized_payload"):
            payload = json.loads(parent[column])
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), parent_id),
            )
        conn.commit()
    finally:
        conn.close()

    def register_worker() -> dict[str, Any]:
        worker = bridge_support.open_database(workspace)
        try:
            return _register(worker)
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda _index: register_worker(), range(2)))
    assert len({receipt["receipt_public_id"] for receipt in receipts}) == 1
    assert sorted(receipt["idempotent_replay"] for receipt in receipts) == [False, True]

    conn = bridge_support.open_database(workspace)
    try:
        prepared = prepare_ai_fallback_v2(
            conn,
            intake_public_id=intake_public_id,
            config_projection=_projection(),
            now_ms=100_000,
        )
    finally:
        conn.close()

    def claim_worker() -> dict[str, Any]:
        worker = bridge_support.open_database(workspace)
        try:
            return claim_ai_fallback_invocation_v2(
                worker, attempt_public_id=prepared["attempt_public_id"], now_ms=100_001
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: claim_worker(), range(2)))
    assert sorted(claim["invocation_disposition"] for claim in claims) == [
        "do_not_invoke",
        "invoke_once",
    ]


def test_prepare_rolls_back_attempt_when_receipt_link_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    try:
        parent = conn.execute(
            "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
            (parent_id,),
        ).fetchone()
        assert parent is not None
        for column in ("parsed_payload", "normalized_payload"):
            payload = json.loads(parent[column])
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), parent_id),
            )
        conn.commit()
        _register(conn)
        monkeypatch.setattr(
            ai_fallback_module,
            "link_attempt_to_receipt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("synthetic link failure")
            ),
        )
        with pytest.raises(AiFallbackServiceError, match="persistence failed"):
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_v2_prepare_claim_result_and_status_are_receipt_pinned(tmp_path: Path) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    try:
        parent = conn.execute(
            "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
            (parent_id,),
        ).fetchone()
        assert parent is not None
        for column in ("parsed_payload", "normalized_payload"):
            payload = json.loads(parent[column])
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), parent_id),
            )
        conn.commit()
        receipt = _register(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(AiFallbackServiceError, match="Foreign keys"):
            prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=_projection(),
                now_ms=100_000,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
        conn.execute("PRAGMA foreign_keys = ON")
        prepared = prepare_ai_fallback_v2(
            conn,
            intake_public_id=intake_public_id,
            config_projection=_projection(),
            now_ms=100_000,
        )
        assert prepared["receipt_public_id"] == receipt["receipt_public_id"]
        assert prepared["request_identity"] == {
            "request_sha256": prepared["request_sha256"],
            "model": "openai/example-model",
            "agent_id": "finance",
            "purpose": "finance-bridge.ai-proposal-v2",
        }
        binding = conn.execute(
            "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
        ).fetchone()[0]
        assert binding == 1
        with pytest.raises(AiFallbackServiceError, match="v2 attempt"):
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        with pytest.raises(AiFallbackServiceError, match="v2 attempt"):
            claim_ai_fallback_invocation(
                conn,
                attempt_public_id=prepared["attempt_public_id"],
                now_ms=100_001,
            )
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(AiFallbackServiceError, match="Foreign keys"):
            claim_ai_fallback_invocation_v2(
                conn, attempt_public_id=prepared["attempt_public_id"], now_ms=100_001
            )
        conn.execute("PRAGMA foreign_keys = ON")
        claim = claim_ai_fallback_invocation_v2(
            conn, attempt_public_id=prepared["attempt_public_id"], now_ms=100_001
        )
        assert claim["model_call"]["model"] == "openai/example-model"
        assert claim["model_call"]["agentId"] == "finance"
        assert claim["model_call"]["purpose"] == "finance-bridge.ai-proposal-v2"
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "outcome_unknown"
        assert status["safe_reason_code"] == "outcome_unknown"
        body, arguments = _response_body(claim)
        arguments.update(
            {
                "returned_model": "example-model",
                "returned_agent_id": "finance",
                "audit_purpose": "finance-bridge.ai-proposal-v2",
            }
        )
        with pytest.raises(AiFallbackServiceError, match="v2 attempt"):
            record_ai_fallback_result_with_disposition(
                conn,
                attempt_public_id=prepared["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(AiFallbackServiceError, match="Foreign keys"):
            record_ai_fallback_result_v2(
                conn,
                attempt_public_id=prepared["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        conn.execute("PRAGMA foreign_keys = ON")
        result, replay = record_ai_fallback_result_v2(
            conn,
            attempt_public_id=prepared["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert replay is False
        assert result["result_status"] == "proposal_created"
        status = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        assert status["processing_path"] == "cloud_projection"
        assert status["safe_reason_code"] is None
        assert status["display_alias"] == "Nomi 云端 🚀"
        assert status["attribution_match"] is True
    finally:
        conn.close()


def test_all_six_v2_bridge_commands_execute_under_existing_envelope(tmp_path: Path) -> None:
    workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    try:
        parent = conn.execute(
            "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
            (parent_id,),
        ).fetchone()
        assert parent is not None
        for column in ("parsed_payload", "normalized_payload"):
            payload = json.loads(parent[column])
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), parent_id),
            )
        conn.commit()
    finally:
        conn.close()
    projection = _projection()
    verified_case = bridge_support.run_cli(
        bridge_support.make_request(
            bridge_envelope.COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2,
            {
                "config_projection": projection,
                "harness_outcome": _outcomes()[0],
            },
        )
    )
    assert verified_case.exit_code == bridge_errors.EXIT_OK
    assert verified_case.response["result"]["case_id"] == "clear_text"
    assert verified_case.response["result"]["verified"] is True
    receipt = bridge_support.run_cli(
        bridge_support.make_request(
            "register_ai_model_compatibility_receipt_v2",
            {
                "workspace_path": str(workspace.workspace_path),
                "config_projection": projection,
                "harness_outcomes": _outcomes(),
            },
            idempotency_key=bridge_commands.canonical_register_ai_model_receipt_v2_key(projection),
        )
    )
    assert receipt.exit_code == bridge_errors.EXIT_OK
    prepared = bridge_support.run_cli(
        bridge_support.make_request(
            "prepare_ai_fallback_v2",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
                "config_projection": projection,
            },
            idempotency_key=bridge_commands.canonical_prepare_ai_fallback_v2_key(intake_public_id),
        )
    )
    assert prepared.exit_code == bridge_errors.EXIT_OK
    attempt_public_id = prepared.response["result"]["attempt_public_id"]
    wrong_claim = bridge_support.run_cli(
        bridge_support.make_request(
            "claim_ai_fallback_invocation",
            {
                "workspace_path": str(workspace.workspace_path),
                "attempt_public_id": attempt_public_id,
            },
            idempotency_key=bridge_commands.canonical_claim_ai_fallback_key(attempt_public_id),
        )
    )
    assert wrong_claim.response["error"]["code"] == "AI_FALLBACK_CONFLICT"
    claimed = bridge_support.run_cli(
        bridge_support.make_request(
            "claim_ai_fallback_invocation_v2",
            {
                "workspace_path": str(workspace.workspace_path),
                "attempt_public_id": attempt_public_id,
            },
            idempotency_key=bridge_commands.canonical_claim_ai_fallback_v2_key(attempt_public_id),
        )
    )
    assert claimed.exit_code == bridge_errors.EXIT_OK
    body, arguments = _response_body(claimed.response["result"])
    arguments.update(
        {
            "returned_model": "example-model",
            "returned_agent_id": "finance",
            "audit_purpose": "finance-bridge.ai-proposal-v2",
        }
    )
    wrong_result = bridge_support.run_cli(
        bridge_support.make_request(
            "record_ai_fallback_result",
            {
                "workspace_path": str(workspace.workspace_path),
                "attempt_public_id": attempt_public_id,
                "transport_outcome": "response_received",
                **arguments,
            },
            idempotency_key=bridge_commands.canonical_record_ai_fallback_key(attempt_public_id),
        )
    )
    assert wrong_result.response["error"]["code"] == "AI_FALLBACK_CONFLICT"
    recorded = bridge_support.run_cli(
        bridge_support.make_request(
            "record_ai_fallback_result_v2",
            {
                "workspace_path": str(workspace.workspace_path),
                "attempt_public_id": attempt_public_id,
                "transport_outcome": "response_received",
                **arguments,
            },
            idempotency_key=bridge_commands.canonical_record_ai_fallback_v2_key(attempt_public_id),
        )
    )
    assert body
    assert recorded.exit_code == bridge_errors.EXIT_OK
    status = bridge_support.run_cli(
        bridge_support.make_request(
            "get_ai_processing_status_v2",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
        )
    )
    assert status.exit_code == bridge_errors.EXIT_OK
    assert status.response["result"]["processing_path"] == "cloud_projection"


@pytest.mark.parametrize("field", ["amount", "currency", "transaction_date", "merchant"])
@pytest.mark.parametrize(
    "bad_refs", [None, True, "t0001", [1], [["t0001"]], ["x9999"], ["t0001", "t0001"]]
)
def test_facts_conflict_evidence_rejects_malformed_without_receipt(
    field: str, bad_refs: Any
) -> None:
    outcome = _outcomes()[0]
    body = json.loads(base64.b64decode(outcome["response_utf8_b64"]))
    body["field_conflicts"][field] = bad_refs
    outcome = _with_response(outcome, json.dumps(body).encode())
    with pytest.raises(ModelCompatibilityError) as exc:
        ai_model_compatibility_module.verify_harness_outcome(_projection(), outcome)
    assert exc.value.details == {"verification_reason": "RESPONSE_SCHEMA_INVALID"}


def _normalize_observations(
    response: dict[str, Any],
    *,
    catalog: dict[str, str],
    parent_values: dict[str, Any],
    parent_flags: list[str] | None = None,
    source_kind: str = "telegram_text",
) -> dict[str, Any]:
    from finance_core.parser_proposals.ai_fact_observations import normalize_fact_observations
    from finance_core.parser_proposals.ai_source_assessment import assess_source

    parent = dict(parent_values)
    if parent_flags is not None:
        parent["ambiguity_flags"] = parent_flags
    return normalize_fact_observations(
        response,
        catalog=catalog,
        assessment=assess_source(catalog=catalog, parent_payload=parent, source_kind=source_kind),
    )


def test_python_derives_ambiguous_fixed_case_without_model_flags_or_conflicts() -> None:
    case = _fixture_cases()[2]
    response = json.loads(_response(case))
    assert "ambiguity_flags" not in response
    assert all(refs == [] for refs in response["field_conflicts"].values())
    normalized = _normalize_observations(
        response,
        catalog=case["catalog"],
        parent_values=case["parent_payload"],
        source_kind=case["source_kind"],
    )
    assert normalized["ambiguity_flags"] == [
        "ambiguous_amount",
        "missing_amount",
        "missing_currency",
        "missing_date",
        "source_conflict",
    ]
    # Model omission and evidence enumeration order cannot resolve Python conflicts.
    reversed_catalog = dict(reversed(list(case["catalog"].items())))
    assert (
        _normalize_observations(
            response,
            catalog=reversed_catalog,
            parent_values=case["parent_payload"],
            source_kind=case["source_kind"],
        )
        == normalized
    )


def test_conflict_observations_only_add_restrictions_and_require_null() -> None:
    case = _fixture_cases()[0]
    body = json.loads(_response(case))
    body["field_conflicts"]["merchant"] = ["t0001"]
    with pytest.raises(ValueError, match="must be null"):
        _normalize_observations(body, catalog=case["catalog"], parent_values={})
    body["merchant"] = None
    body["field_evidence_refs"]["merchant"] = []
    body["field_confidence_bps"]["merchant"] = None
    result = _normalize_observations(body, catalog=case["catalog"], parent_values={})
    assert result["ambiguity_flags"] == [
        "ambiguous_merchant",
        "missing_date",
        "missing_merchant_or_description",
        "source_conflict",
    ]


def test_whole_catalog_and_parent_conflicts_survive_empty_observations() -> None:
    body = json.loads(_response(_fixture_cases()[0]))
    result = _normalize_observations(
        body,
        catalog={"t0001": "Paid SGD 12.34 at Cafe", "t0002": "JPY 20"},
        parent_values={},
        parent_flags=["conflicting_date_candidates"],
    )
    assert result["ambiguity_flags"] == [
        "ambiguous_amount",
        "ambiguous_currency",
        "ambiguous_date",
        "missing_date",
        "source_conflict",
    ]


def test_missing_date_and_date_conflict_are_python_owned() -> None:
    body = json.loads(_response(_fixture_cases()[0]))
    first = _normalize_observations(
        body,
        catalog={"t0001": "SGD 12.34 at Cafe on 2026-08-13, 2 items"},
        parent_values={},
    )
    assert first["ambiguity_flags"] == ["missing_date"]
    inherited = _normalize_observations(
        body,
        catalog={"t0001": "SGD 12.34 at Cafe on 2026-08-13"},
        parent_values={"transaction_date": "2026-08-13"},
    )
    assert inherited["ambiguity_flags"] == []
    conflict = _normalize_observations(
        body,
        catalog={"t0001": "SGD 12.34 at Cafe on 2026-08-13 or 2026-08-14"},
        parent_values={},
    )
    assert conflict["ambiguity_flags"] == ["ambiguous_date", "missing_date", "source_conflict"]


@pytest.mark.parametrize("case", _fixture_cases()[1:], ids=lambda case: case["case_id"])
def test_fixed_ocr_parent_context_matches_real_total_parser(case: dict[str, Any]) -> None:
    from finance_core.parser_proposals.receipt_total_parser import (
        ParserOcrBlock,
        parse_receipt_total,
    )

    blocks = tuple(
        ParserOcrBlock(
            sequence_index=index,
            page_index=0,
            text=text,
            left=0,
            top=index * 100,
            engine_line_index=index,
            confidence_scaled=9500,
        )
        for index, text in enumerate(case["catalog"].values())
    )
    parsed = parse_receipt_total(blocks, extraction_status="succeeded")
    parent = case["parent_payload"]
    assert parent["ambiguity_flags"] == list(parsed.ambiguity_flags)
    for field in ("amount", "currency", "transaction_date", "merchant"):
        parsed_field = getattr(parsed, field)
        assert parent[field] == parsed_field.value
        evidence = [row for row in parent["field_evidence"] if row["field_name"] == field]
        if parsed_field.value is None:
            assert evidence == []
        else:
            assert len(evidence) == 1
            assert evidence[0]["proposed_value"] == parsed_field.value
            assert evidence[0]["block_sequence_indexes"] == list(
                parsed_field.block_sequence_indexes
            )
            assert (
                evidence[0]["normalized_result_hash"]
                == parent["ocr_evidence"]["normalized_result_hash"]
            )
            assert (
                evidence[0]["extraction_public_id"]
                == parent["ocr_evidence"]["extraction_public_id"]
            )


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["case_id"])
def test_gate5_and_proposals_admit_same_fixed_observations(case: dict[str, Any]) -> None:
    from finance_core.parser_proposals.ai_response_validation import validate_ai_response

    assert ai_fallback_module._validate_ai_response is validate_ai_response
    assert ai_model_compatibility_module.validate_ai_response is validate_ai_response
    body = _response(case)
    result = validate_ai_response(
        body,
        set(case["catalog"]),
        parent_payload=case["parent_payload"],
        catalog=case["catalog"],
        source_kind=case["source_kind"],
    )
    assert "acceptance_policy" not in result
    for field, expected in case["expected"].items():
        if field != "acceptance_policy":
            assert result[field] == expected
    ai_model_compatibility_module._verify_response(case, body)


@pytest.mark.parametrize("tamper", ["no_parent", "unbound_extraction", "wrong_block_refs"])
def test_gate5_rejects_ocr_that_actual_proposal_admission_refuses(tamper: str) -> None:
    case = _fixture_cases()[1]
    body = _response(case)
    if tamper == "no_parent":
        case["parent_payload"] = {}
    elif tamper == "unbound_extraction":
        case["parent_payload"]["ocr_evidence"]["normalized_result_hash"] = "0" * 64
    else:
        response = json.loads(body)
        response["field_evidence_refs"]["amount"] = ["e0002"]
        response["field_evidence_refs"]["currency"] = ["e0002"]
        body = json.dumps(response).encode()
    with pytest.raises(ValueError, match="one evidence pair"):
        ai_fallback_module._validate_ai_response(
            body,
            set(case["catalog"]),
            parent_payload=case["parent_payload"],
            catalog=case["catalog"],
            source_kind=case["source_kind"],
        )
    with pytest.raises(ai_model_compatibility_module._VerificationRefusal) as exc:
        ai_model_compatibility_module._verify_response(case, body)
    assert exc.value.reason == "RESPONSE_ADMISSION_INVALID"


@pytest.mark.parametrize(
    "tamper", ["wrong_money", "extra_money", "wrong_merchant", "extra_merchant"]
)
def test_shared_admission_refusal_writes_no_compatibility_receipt(tamper: str) -> None:
    conn = _connection()
    try:
        outcomes = _outcomes()
        response = json.loads(base64.b64decode(outcomes[1]["response_utf8_b64"]))
        if tamper in {"wrong_money", "extra_money"}:
            for field in ("amount", "currency"):
                response["field_evidence_refs"][field] = (
                    ["e0002"] if tamper == "wrong_money" else ["e0003", "e0002"]
                )
        else:
            response["field_evidence_refs"]["merchant"] = (
                ["e0002"] if tamper == "wrong_merchant" else ["e0001", "e0002"]
            )
        case = _fixture_cases()[1]
        raw = json.dumps(response).encode()
        with pytest.raises(ValueError, match="evidence"):
            ai_fallback_module._validate_ai_response(
                raw,
                set(case["catalog"]),
                parent_payload=case["parent_payload"],
                catalog=case["catalog"],
                source_kind=case["source_kind"],
            )
        outcomes[1] = _with_response(outcomes[1], json.dumps(response).encode())
        with pytest.raises(ModelCompatibilityError) as exc:
            register_ai_model_compatibility_receipt_v2(
                conn,
                config_projection=_projection(),
                harness_outcomes=outcomes,
                now_ms=1000,
            )
        assert exc.value.details == {"verification_reason": "RESPONSE_ADMISSION_INVALID"}
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize("source_kind", ["telegram_text", "telegram_raw_text"])
def test_fixture_and_production_text_source_names_share_admission(source_kind: str) -> None:
    case = _fixture_cases()[0]
    result = ai_fallback_module._validate_ai_response(
        _response(case),
        set(case["catalog"]),
        parent_payload={},
        catalog=case["catalog"],
        source_kind=source_kind,
    )
    assert result["amount"] == "12.34"
    assert result["currency"] == "SGD"
    assert result["ambiguity_flags"] == ["missing_date"]


def test_shared_admission_refuses_unknown_source_kind() -> None:
    case = _fixture_cases()[0]
    with pytest.raises(ValueError, match="source kind is unsupported"):
        ai_fallback_module._validate_ai_response(
            _response(case),
            set(case["catalog"]),
            parent_payload={},
            catalog=case["catalog"],
            source_kind="unknown_source",
        )


@pytest.mark.parametrize("field", ["amount", "currency", "transaction_date", "merchant"])
def test_text_admission_rejects_extra_unrelated_refs_for_every_field(field: str) -> None:
    case = _fixture_cases()[0]
    case["catalog"]["t0001"] += " on 2026-08-13"
    case["catalog"]["t0002"] = "unrelated note"
    response = json.loads(_response(case))
    response["transaction_date"] = "2026-08-13"
    response["field_confidence_bps"]["transaction_date"] = 9000
    response["field_evidence_refs"]["transaction_date"] = ["t0001"]
    response["field_evidence_refs"][field].append("t0002")
    with pytest.raises(ValueError, match="evidence"):
        ai_fallback_module._validate_ai_response(
            json.dumps(response).encode(),
            set(case["catalog"]),
            parent_payload=case["parent_payload"],
            catalog=case["catalog"],
            source_kind=case["source_kind"],
        )


@pytest.mark.parametrize("currency", ["sgd", " SGD "])
def test_shared_admission_requires_canonical_wire_currency(currency: str) -> None:
    case = _fixture_cases()[0]
    response = json.loads(_response(case))
    response["currency"] = currency
    raw = json.dumps(response).encode()
    with pytest.raises(ValueError, match="currency is not canonical"):
        ai_fallback_module._validate_ai_response(
            raw,
            set(case["catalog"]),
            parent_payload=case["parent_payload"],
            catalog=case["catalog"],
            source_kind=case["source_kind"],
        )
    # The fixed oracle diagnoses value mismatches before shared admission.
    with pytest.raises(ai_model_compatibility_module._VerificationRefusal) as exc:
        ai_model_compatibility_module._verify_response(case, raw)
    assert exc.value.reason == "FIELD_MISMATCH"
    assert exc.value.field == "currency"
    # Even a future oracle expecting that spelling cannot bypass shared admission.
    case["expected"]["currency"] = currency
    with pytest.raises(ai_model_compatibility_module._VerificationRefusal) as exc:
        ai_model_compatibility_module._verify_response(case, raw)
    assert exc.value.reason == "RESPONSE_ADMISSION_INVALID"
