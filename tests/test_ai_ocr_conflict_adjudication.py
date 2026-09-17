"""Real-response regression and adversarial OCR layout role proofs."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    _normalized_outcome,
)
from finance_core.parser_proposals.ai_model_compatibility import (
    ModelCompatibilityError,
    _verify_response,
    register_ai_model_compatibility_receipt_v2,
)
from finance_core.parser_proposals.ai_ocr_layout import verify_ocr_layout
from finance_core.parser_proposals.ai_response_validation import validate_ai_response
from finance_core.parser_proposals.receipt_total_parser import (
    ParserOcrBlock,
    explicit_item_line_groups,
)
from tests.test_ai_model_compatibility_receipts_v2 import (
    _connection,
    _fixture_cases,
    _outcomes,
    _projection,
    _response,
    _with_response,
)


def _case_response() -> tuple[dict[str, Any], dict[str, Any]]:
    case = _fixture_cases()[1]
    body = json.loads(_response(case))
    # Captured real Luna response had the right total but compared ITEM and TOTAL.
    body["field_conflicts"]["amount"] = ["e0002", "e0003"]
    return case, body


def _admit(case: dict[str, Any], body: dict[str, Any], *, layout: bool = True) -> dict[str, Any]:
    return validate_ai_response(
        json.dumps(body).encode(),
        set(case["catalog"]),
        catalog=case["catalog"],
        parent_payload=case["parent_payload"],
        source_kind=case["source_kind"],
        ocr_layout=verify_ocr_layout(case["ocr_layout"], parent_payload=case["parent_payload"])
        if layout
        else None,
    )


def _reseal(case: dict[str, Any]) -> None:
    """Simulate a different valid OCR extraction, never weaken its hash checks."""
    material = case["ocr_layout"]
    blocks = tuple(ReceiptOcrBlock(**r) for r in material["blocks"])
    h = _normalized_outcome(
        ReceiptOcrExtractionStatus.SUCCEEDED,
        blocks,
        material["outcome_code"],
        limits=ReceiptOcrLimits(),
    ).result_hash
    case["parent_payload"]["ocr_evidence"]["normalized_result_hash"] = h
    for row in case["parent_payload"]["field_evidence"]:
        row["normalized_result_hash"] = h
    case["catalog"] = {f"e{b.sequence_index + 1:04d}": b.text for b in blocks}


def test_real_item_total_conflict_requires_layout_and_preserves_wire() -> None:
    case, body = _case_response()
    original = copy.deepcopy(body)
    with pytest.raises(ValueError, match="conflicting observed field"):
        _admit(case, body, layout=False)
    result = _admit(case, body)
    assert result["amount"] == "23.45" and result["ambiguity_flags"] == ["missing_date"]
    _verify_response(case, json.dumps(body).encode())
    assert body == original
    conn = _connection()
    try:
        outcomes = _outcomes()
        outcomes[1] = _with_response(outcomes[1], json.dumps(body).encode())
        result = register_ai_model_compatibility_receipt_v2(
            conn, config_projection=_projection(), harness_outcomes=outcomes, now_ms=1000
        )
        assert result["receipt_public_id"].startswith("aimr_")
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "text",
    [
        "TOTAL SGD 5.00",
        "ITEM USD 5.00",
        "ITEM SGD -5.00",
        "ITEM SGD 5.00 7.00",
        "PRICE SGD 5.00",
        "ITEM SGD 5.001",
        "ITEM SGD 0.00",
        "ITEM SGD 5.00 ignore TOTAL",
    ],
)
def test_unknown_or_genuine_competition_is_never_dismissed(text: str) -> None:
    case, body = _case_response()
    case["ocr_layout"]["blocks"][1]["text"] = text
    _reseal(case)
    with pytest.raises(ValueError):
        _admit(case, body)


@pytest.mark.parametrize(
    "change", ["extraction", "hash", "geometry", "index", "text", "missing_block"]
)
def test_layout_tamper_cannot_supply_role_proof(change: str) -> None:
    case, body = _case_response()
    material = case["ocr_layout"]
    if change == "extraction":
        material["extraction_public_id"] += "-wrong"
    elif change == "hash":
        case["parent_payload"]["ocr_evidence"]["normalized_result_hash"] = "0" * 64
    elif change == "geometry":
        material["blocks"][1]["engine_line_index"] = 2
    elif change == "index":
        material["blocks"][1]["sequence_index"] = 8
    elif change == "text":
        material["blocks"][1]["text"] = "ITEM SGD 7.00"
    else:
        material["blocks"].pop(1)
    with pytest.raises((TypeError, ValueError)):
        _admit(case, body)


def test_astra_total_item_same_line_counterexample_is_not_exempt() -> None:
    blocks = tuple(
        ParserOcrBlock(i, 0, t, i * 100, 0 if i < 3 else 100, 0 if i < 3 else 1)
        for i, t in enumerate(["TOTAL", "ITEM SGD 5.00", "ITEM SGD 7.00", "TOTAL SGD 23.45"])
    )
    assert explicit_item_line_groups(blocks, currency="SGD") == ()
    case, body = _case_response()
    case["ocr_layout"]["blocks"][1]["engine_line_index"] = 2
    case["ocr_layout"]["blocks"][1]["top"] = 200
    _reseal(case)
    with pytest.raises(ValueError):
        _admit(case, body)


@pytest.mark.parametrize(
    "change",
    [
        "parent_conflict",
        "amount",
        "currency",
        "null",
        "item_as_amount_evidence",
        "currency_conflict",
        "unknown_ref",
        "duplicate_ref",
        "only_total",
    ],
)
def test_role_proof_cannot_override_other_admission_rules(change: str) -> None:
    case, body = _case_response()
    if change == "parent_conflict":
        case["parent_payload"]["ambiguity_flags"].append("conflicting_total_candidates")
    elif change == "amount":
        body["amount"] = "5.00"
    elif change == "currency":
        body["currency"] = "USD"
    elif change == "null":
        body["amount"] = None
    elif change == "item_as_amount_evidence":
        body["field_evidence_refs"]["amount"].append("e0002")
    elif change == "currency_conflict":
        body["field_conflicts"]["currency"] = ["e0002", "e0003"]
    elif change == "unknown_ref":
        body["field_conflicts"]["amount"].append("e9999")
    elif change == "duplicate_ref":
        body["field_conflicts"]["amount"].append("e0002")
    else:
        body["field_conflicts"]["amount"] = ["e0003"]
    with pytest.raises(ValueError):
        _admit(case, body)


def test_bad_later_case_keeps_receipt_atomic_after_item_role_acceptance() -> None:
    case, body = _case_response()
    conn = _connection()
    try:
        outcomes = _outcomes()
        outcomes[1] = _with_response(outcomes[1], json.dumps(body).encode())
        later = json.loads(__import__("base64").b64decode(outcomes[3]["response_utf8_b64"]))
        later["amount"] = "99.99"
        outcomes[3] = _with_response(outcomes[3], json.dumps(later).encode())
        with pytest.raises(ModelCompatibilityError):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=outcomes, now_ms=1000
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def _tokenized_case() -> tuple[dict[str, Any], dict[str, Any]]:
    case, body = _case_response()
    template = case["ocr_layout"]["blocks"][0]
    tokens = [
        ("Market", 0, 0),
        ("ITEM", 1, 0),
        ("SGD", 1, 50),
        ("5.00", 1, 100),
        ("TOTAL", 2, 0),
        ("SGD", 2, 50),
        ("23.45", 2, 100),
    ]
    case["ocr_layout"]["blocks"] = [
        {
            **template,
            "sequence_index": i,
            "text": text,
            "engine_line_index": line,
            "left": left,
            "top": line * 100,
        }
        for i, (text, line, left) in enumerate(tokens)
    ]
    for field in case["parent_payload"]["field_evidence"]:
        if field["field_name"] in ("amount", "currency"):
            field["block_sequence_indexes"] = [4, 5, 6]
    for field in ("amount", "currency"):
        body["field_evidence_refs"][field] = ["e0005", "e0006", "e0007"]
    body["field_conflicts"]["amount"] = [f"e{i:04d}" for i in range(2, 8)]
    _reseal(case)
    return case, body


def test_complete_tokenized_item_and_total_evidence_is_accepted() -> None:
    case, body = _tokenized_case()
    assert _admit(case, body)["ambiguity_flags"] == ["missing_date"]
    _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize(
    "refs",
    [
        ["e0002", "e0005"],  # labels alone contain neither amount
        ["e0002", "e0003", "e0005", "e0006", "e0007"],
        ["e0004", "e0007"],
    ],
)
def test_partial_tokenized_comparisons_are_refused(refs: list[str]) -> None:
    case, body = _tokenized_case()
    body["field_conflicts"]["amount"] = refs
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize(
    "collision", ["block", "paragraph", "vision", "vertical", "total", "missing"]
)
def test_structural_line_collisions_never_prove_item_or_total(collision: str) -> None:
    case, body = _tokenized_case()
    blocks = case["ocr_layout"]["blocks"]
    if collision == "block":
        blocks[2]["engine_block_index"] = 8
    elif collision == "paragraph":
        blocks[2]["engine_paragraph_index"] = 8
    elif collision == "vision":
        for index in range(1, 4):
            blocks[index]["engine_line_index"] = None
            blocks[index]["engine_block_index"] = index
    elif collision == "vertical":
        blocks[2]["top"] = 500
    elif collision == "total":
        blocks[5]["engine_paragraph_index"] = 8
    else:
        blocks[2]["engine_block_index"] = None
    _reseal(case)
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


def test_partial_second_item_group_is_not_hidden_by_one_complete_group() -> None:
    case, body = _tokenized_case()
    template = case["ocr_layout"]["blocks"][0]
    case["ocr_layout"]["blocks"] += [
        {
            **template,
            "sequence_index": 7 + i,
            "text": text,
            "engine_line_index": 3,
            "top": 300,
            "left": i * 50,
        }
        for i, text in enumerate(["ITEM", "SGD", "7.00"])
    ]
    _reseal(case)
    body["field_conflicts"]["amount"].append("e0008")
    with pytest.raises(ValueError):
        _admit(case, body)
    body["field_conflicts"]["amount"] += ["e0009", "e0010"]
    assert _admit(case, body)["ambiguity_flags"] == ["missing_date"]


def test_legal_custom_limit_layout_retains_its_original_hash() -> None:
    case, body = _case_response()
    for block in case["ocr_layout"]["blocks"]:
        block["page_width"] = 60_000
    blocks = tuple(ReceiptOcrBlock(**row) for row in case["ocr_layout"]["blocks"])
    sealed = _normalized_outcome(
        ReceiptOcrExtractionStatus.SUCCEEDED,
        blocks,
        "ok",
        limits=ReceiptOcrLimits(max_image_width=60_000),
    )
    case["parent_payload"]["ocr_evidence"]["normalized_result_hash"] = sealed.result_hash
    for field in case["parent_payload"]["field_evidence"]:
        field["normalized_result_hash"] = sealed.result_hash
    assert _admit(case, body)["amount"] == "23.45"


def test_all_canonical_quoted_basis_points_decode_without_rounding() -> None:
    from finance_core.parser_proposals.ai_fact_observations import _decode_confidence_bps

    for value in range(10_001):
        assert _decode_confidence_bps(str(value)) == value
        assert type(_decode_confidence_bps(str(value))) is int
        assert _decode_confidence_bps(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "9500 ",
        " 9500",
        "9500\n",
        "+9500",
        "-1",
        "-0",
        "09500",
        "00",
        "9500.0",
        "9.5e3",
        "95%",
        "0.95",
        "10001",
        "9999999999999999999999999",
        "９５００",
        "٩٥٠٠",
        "null",
        "NaN",
        "Infinity",
        True,
        False,
        9500.0,
        -1,
        10001,
        [],
        {},
    ],
)
def test_confidence_decoder_never_guesses_noncanonical_values(value: Any) -> None:
    case, body = _case_response()
    body["field_confidence_bps"]["amount"] = value
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


def test_captured_quoted_confidence_passes_shared_admission_without_mutating_wire() -> None:
    from finance_core.parser_proposals.ai_fact_observations import normalize_fact_observations
    from finance_core.parser_proposals.ai_source_assessment import assess_source

    case, body = _case_response()
    body["field_confidence_bps"].update(amount="9500", currency="9500", merchant="7000")
    before = copy.deepcopy(body)
    normalized = normalize_fact_observations(
        body,
        catalog=case["catalog"],
        assessment=assess_source(
            catalog=case["catalog"],
            parent_payload=case["parent_payload"],
            source_kind=case["source_kind"],
            ocr_layout=verify_ocr_layout(case["ocr_layout"], parent_payload=case["parent_payload"]),
        ),
    )
    assert body == before
    assert normalized["field_confidence_bps"]["amount"] == 9500
    assert _admit(case, body)["amount"] == "23.45"
    _verify_response(case, json.dumps(body).encode())


def test_quoted_confidence_receipt_preserves_original_response_hashes() -> None:
    import base64
    import hashlib

    outcomes = _outcomes()
    expected_hashes = []
    for i, outcome in enumerate(outcomes):
        body = json.loads(base64.b64decode(outcome["response_utf8_b64"]))
        body["field_confidence_bps"] = {
            field: str(value) if value is not None else None
            for field, value in body["field_confidence_bps"].items()
        }
        raw = json.dumps(body).encode()
        outcomes[i] = _with_response(outcome, raw)
        expected_hashes.append(hashlib.sha256(raw).hexdigest())
    conn = _connection()
    try:
        register_ai_model_compatibility_receipt_v2(
            conn,
            config_projection=_projection(),
            harness_outcomes=outcomes,
            now_ms=1000,
        )
        results = json.loads(
            conn.execute(
                "SELECT fixture_results_json FROM ai_model_compatibility_receipts"
            ).fetchone()[0]
        )
        assert [result["response_sha256"] for result in results] == expected_hashes
    finally:
        conn.close()


def test_confidence_decoding_never_converts_numeric_money_or_lowers_other_checks() -> None:
    case, body = _case_response()
    body["field_confidence_bps"]["amount"] = "9500"
    body["amount"] = 23.45
    with pytest.raises(ValueError):
        _admit(case, body)
    body["amount"] = "99.99"
    with pytest.raises(ValueError):
        _admit(case, body)


@pytest.mark.parametrize("omission_mask", range(16))
def test_null_conflict_omissions_preserve_source_restrictions(omission_mask: int) -> None:
    case = _fixture_cases()[2]
    body = json.loads(_response(case))
    body["merchant"] = None
    body["field_confidence_bps"]["merchant"] = None
    body["field_evidence_refs"]["merchant"] = []
    baseline = _admit(case, body, layout=False)
    for bit, field in enumerate(("amount", "currency", "transaction_date", "merchant")):
        if omission_mask & (1 << bit):
            assert body[field] is None
            body["field_conflicts"].pop(field)
    before = copy.deepcopy(body)
    admitted = _admit(case, body, layout=False)
    assert admitted == baseline
    assert {"ambiguous_amount", "source_conflict"} <= set(admitted["ambiguity_flags"])
    _verify_response(case, json.dumps(body).encode())
    assert body == before


@pytest.mark.parametrize("field", ["amount", "currency", "transaction_date", "merchant"])
def test_conflict_omission_cannot_certify_a_present_value(field: str) -> None:
    case = _fixture_cases()[2]
    body = json.loads(_response(case))
    values = {
        "amount": "12.34",
        "currency": "SGD",
        "transaction_date": "2026-09-08",
        "merchant": "Cafe",
    }
    body[field] = values[field]
    body["field_conflicts"].pop(field)
    with pytest.raises(ValueError, match="requires conflict observations"):
        _admit(case, body, layout=False)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize(
    "change",
    ["unknown_key", "null_map", "null_refs", "unknown_ref", "duplicate_ref", "missing_map"],
)
def test_sparse_conflicts_still_refuse_malformed_or_untrusted_observations(change: str) -> None:
    case = _fixture_cases()[2]
    body = json.loads(_response(case))
    body["field_conflicts"].pop("transaction_date")
    if change == "unknown_key":
        body["field_conflicts"]["date"] = []
    elif change == "null_map":
        body["field_conflicts"] = None
    elif change == "null_refs":
        body["field_conflicts"]["amount"] = None
    elif change == "unknown_ref":
        body["field_conflicts"]["amount"] = ["e9999"]
    elif change == "duplicate_ref":
        body["field_conflicts"]["amount"] = ["e0002", "e0002"]
    else:
        body.pop("field_conflicts")
    with pytest.raises(ValueError):
        _admit(case, body, layout=False)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


def test_sparse_conflicts_receipt_retains_original_response_hash() -> None:
    import hashlib

    case = _fixture_cases()[2]
    body = json.loads(_response(case))
    body["merchant"] = None
    body["field_confidence_bps"]["merchant"] = None
    body["field_evidence_refs"]["merchant"] = []
    body["field_conflicts"]["amount"] = ["e0002", "e0003"]
    body["field_conflicts"]["currency"] = ["e0002", "e0003"]
    body["field_conflicts"].pop("transaction_date")
    body["field_conflicts"].pop("merchant")
    raw = json.dumps(body).encode()
    outcomes = _outcomes()
    outcomes[2] = _with_response(outcomes[2], raw)
    conn = _connection()
    try:
        register_ai_model_compatibility_receipt_v2(
            conn, config_projection=_projection(), harness_outcomes=outcomes, now_ms=1000
        )
        results = json.loads(
            conn.execute(
                "SELECT fixture_results_json FROM ai_model_compatibility_receipts"
            ).fetchone()[0]
        )
        assert results[2]["response_sha256"] == hashlib.sha256(raw).hexdigest()
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "money",
        "merchant",
        "intent",
        "null_evidence",
        "unknown_policy",
        "positive_policy",
        "missing_required_policy_flags",
    ],
)
def test_negative_acceptance_does_not_authorize_unsafe_or_unrelated_outputs(change: str) -> None:
    case = _fixture_cases()[2]
    body = json.loads(_response(case))
    body["field_conflicts"]["currency"] = ["e0002", "e0003"]
    if change == "money":
        body.update(amount="10.00", currency="SGD")
        body["field_evidence_refs"].update(amount=["e0002"], currency=["e0002"])
    elif change == "merchant":
        body["merchant"] = "Other Store"
    elif change == "intent":
        body["intent_type"] = "unknown"
    elif change == "null_evidence":
        body["field_evidence_refs"]["amount"] = ["e0002"]
    elif change == "unknown_policy":
        case["expected"]["acceptance_policy"] = "allow-anything"
    elif change == "positive_policy":
        case = _fixture_cases()[0]
        body = json.loads(_response(case))
        case["expected"]["acceptance_policy"] = "conservative-abstention-v1"
    else:
        case["expected"]["ambiguity_flags"].remove("source_conflict")
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize("case_index", [0, 1, 3])
@pytest.mark.parametrize("change", ["all_null", "extra_conflict"])
def test_positive_cases_keep_exact_capability_requirements(case_index: int, change: str) -> None:
    case = _fixture_cases()[case_index]
    body = json.loads(_response(case))
    if change == "all_null":
        for field in body["field_confidence_bps"]:
            body[field] = None
            body["field_confidence_bps"][field] = None
            body["field_evidence_refs"][field] = []
    else:
        body["transaction_date"] = None
        body["field_conflicts"]["transaction_date"] = [next(iter(case["catalog"]))]
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize("total_mask", range(8))
def test_complete_value_total_need_not_repeat_in_conflict_list(total_mask: int) -> None:
    case, body = _tokenized_case()
    body["field_conflicts"]["amount"] = ["e0002", "e0003", "e0004"] + [
        ref for i, ref in enumerate(["e0005", "e0006", "e0007"]) if total_mask & (1 << i)
    ]
    before = copy.deepcopy(body)
    assert _admit(case, body)["ambiguity_flags"] == ["missing_date"]
    _verify_response(case, json.dumps(body).encode())
    assert body == before


@pytest.mark.parametrize("item_mask", range(7))
def test_partial_item_cannot_borrow_completeness_from_value_refs(item_mask: int) -> None:
    case, body = _tokenized_case()
    body["field_conflicts"]["amount"] = ["e0005", "e0006", "e0007"] + [
        ref for i, ref in enumerate(["e0002", "e0003", "e0004"]) if item_mask & (1 << i)
    ]
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize("value_mask", range(7))
def test_partial_value_total_cannot_borrow_completeness_from_conflicts(value_mask: int) -> None:
    case, body = _tokenized_case()
    refs = [ref for i, ref in enumerate(["e0005", "e0006", "e0007"]) if value_mask & (1 << i)]
    for field in ("amount", "currency"):
        body["field_evidence_refs"][field] = refs
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


@pytest.mark.parametrize(
    "refs", [None, [], ["e0003", "e0003"], ["e0003", "e0002"], ["e9999"], [1], [["e0003"]]]
)
def test_value_total_proof_rejects_malformed_duplicate_or_foreign_refs(refs: Any) -> None:
    case, body = _case_response()
    body["field_conflicts"]["amount"] = ["e0002"]
    body["field_evidence_refs"]["amount"] = refs
    with pytest.raises(ValueError):
        _admit(case, body)
    with pytest.raises(ValueError):
        _verify_response(case, json.dumps(body).encode())


def test_item_only_conflict_preserves_raw_receipt_hash_and_atomic_failure() -> None:
    import base64
    import hashlib

    case, body = _case_response()
    body["field_conflicts"]["amount"] = ["e0002"]
    raw = json.dumps(body).encode()
    outcomes = _outcomes()
    outcomes[1] = _with_response(outcomes[1], raw)
    conn = _connection()
    try:
        bad = copy.deepcopy(outcomes)
        last = json.loads(base64.b64decode(bad[3]["response_utf8_b64"]))
        last["amount"] = "99.99"
        bad[3] = _with_response(bad[3], json.dumps(last).encode())
        with pytest.raises(ModelCompatibilityError):
            register_ai_model_compatibility_receipt_v2(
                conn, config_projection=_projection(), harness_outcomes=bad, now_ms=1000
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
        register_ai_model_compatibility_receipt_v2(
            conn, config_projection=_projection(), harness_outcomes=outcomes, now_ms=1000
        )
        result = json.loads(
            conn.execute(
                "SELECT fixture_results_json FROM ai_model_compatibility_receipts"
            ).fetchone()[0]
        )
        assert result[1]["response_sha256"] == hashlib.sha256(raw).hexdigest()
    finally:
        conn.close()
