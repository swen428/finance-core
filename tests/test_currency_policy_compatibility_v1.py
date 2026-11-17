"""Frozen pre-MYR synthetic parser results and proposal hash compatibility.

Expected hashes were captured before changing the Money registry. The parser
DTO shape, raw excerpts, unresolved fields and evidence indexes all participate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from finance_core.application.admission import TrustedBinding
from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import InterpretationPolicy, ProviderBinding
from finance_core.application.corrections import CorrectionError
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2 import VerifiedSource as LifecycleSource
from finance_core.lifecycle_v2.service import _money as native_expense_money
from finance_core.money import MoneyValidationError
from finance_core.parser_proposals.ai_source_assessment import assess_source
from finance_core.parser_proposals.content_hash import (
    canonicalize_proposal_money,
    compute_proposal_content_hash,
)
from finance_core.parser_proposals.decision_owner import (
    ProposalConversionError,
    _required_canonical_money,
)
from finance_core.parser_proposals.human_draft_validation import (
    HumanDraftValidationError,
    canonicalize_human_draft_before,
    validate_human_draft,
)
from finance_core.parser_proposals.independent_ai_source_grammar import assess_source_money
from finance_core.parser_proposals.independent_ai_v2 import _canonical_fields
from finance_core.parser_proposals.receipt_total_parser import (
    ParserOcrBlock,
    explicit_item_line_groups,
    parse_receipt_total,
)
from tests import test_correction_core_v1 as coupled_corrections
from tests import test_independent_corrections_v1 as independent_corrections
from tests.test_lifecycle_v2 import live as live
from tests.test_lifecycle_v2 import response as lifecycle_response


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(
    "marker,expected",
    [
        ("MYR", "e09942fe1a159d01c6d7c8f707086e6c783d927bbb52a1316df6fd14588464e5"),
        ("RM", "a42221a911be81c2f3804dee700dae826e724ac165a121560f8b663042921a6e"),
        ("SGD", "833ff265031ef53402a7f8e5e40cee369d6b422d9deffd0ab832ec78c9e6cd08"),
    ],
)
def test_historical_receipt_results_rebuild_exactly(version, marker, expected):
    blocks = tuple(
        ParserOcrBlock(
            i,
            0,
            text,
            0,
            i * 30,
            engine_line_index=i,
            engine_block_index=0,
            engine_paragraph_index=0,
            height=10,
        )
        for i, text in enumerate(("SYNTHETIC CAFE", "2026-01-02", f"TOTAL {marker} 12.30"))
    )
    parsed = parse_receipt_total(blocks, extraction_status="succeeded", parser_version=version)
    canonical = json.dumps(asdict(parsed), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == expected
    if marker != "SGD":
        assert parsed.amount.value is None
        assert parsed.currency.value == "MYR"
        assert parsed.ambiguity_flags == ("unsupported_currency_for_amount",)


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("MYR", "fc9a6d382c92e5d3f9a480c11bd46b9ad9e4432971996f63f3c09cce32078b9d"),
        ("RM", "add973f30c832b1c45054b59a4a5be49cef58223857c880e6a821f4ceae3efea"),
        ("ＭＹＲ", "9ffce299d7699f888dbeee7fd1654c70d60c1648b2a01eed446be59a3d01521c"),
    ],
)
def test_historical_ai_grammar_rebuilds_exactly(marker, expected):
    raw = f"Coffee {marker} 12.30 at Cafe"
    result = assess_source_money(raw)
    material = asdict(result)
    material["currencies"] = sorted(material["currencies"])
    material["reasons"] = sorted(material["reasons"])
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == expected
    assert not result.qualified
    assert result.currency_state == "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
    assessment = assess_source(catalog={"raw": raw}, parent_payload={}, source_kind="telegram_text")
    assert assessment.parent_values["amount"] is None
    assert assessment.parent_values["currency"] is None


@pytest.mark.parametrize(
    "code,expected",
    [
        ("MYR", "41d001cf31e11f61e16b31709f0cceef0440b997f106508e84cf474c67949215"),
        (" myr ", "6c0d56c0329dfa15b7180910d2ac104e49824739598c2a76bcc8f8450e9bdca4"),
        ("SGD", "92163e12ec414a18e6fe68496ee40fd7f6b5a7171066c7bf0a588b4bb862996f"),
    ],
)
def test_historical_proposal_content_hash_stays_exact(code, expected):
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE attachments(public_id TEXT, file_path TEXT, file_hash TEXT, "
            "parser_output_id INTEGER, id INTEGER)"
        )
        proposal = {
            "id": 1,
            "public_id": "proposal-synthetic",
            "source_type": "telegram_text",
            "source_public_id": "source-synthetic",
            "statement_batch_id": None,
            "raw_text": f"Coffee {code} 12.3",
            "parser_name": "text-expense-parser",
            "parser_version": "v1",
            "attachment_id": None,
            "parsed_payload": json.dumps({"amount": "12.3", "currency": code}),
        }
        assert compute_proposal_content_hash(conn, proposal) == expected


def test_historical_myr_remains_invalid_for_proposal_conversion():
    with pytest.raises(MoneyValidationError, match="Unsupported currency 'MYR'"):
        canonicalize_proposal_money("12.3", "MYR")
    with pytest.raises(ProposalConversionError, match="Invalid parser proposal monetary fields"):
        _required_canonical_money({"amount": "12.3", "currency": "MYR"})


def test_historical_human_snapshot_keeps_unsupported_raw_money():
    original = {"amount": "12.3", "currency": " myr "}
    assert canonicalize_human_draft_before(original, source_type="telegram_text") == original


def test_historical_human_completion_cannot_start_accepting_myr():
    with pytest.raises(HumanDraftValidationError, match="D1_CURRENCY_INVALID"):
        validate_human_draft(
            {},
            {"amount": "12.30", "currency": "MYR"},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="synthetic-completion",
        )


def test_historical_ai_v2_partial_draft_cannot_gain_myr_support():
    with pytest.raises(MoneyValidationError, match="Unsupported currency 'MYR'"):
        _canonical_fields({"amount": "12.3", "currency": "MYR"}, complete=False)


def test_historical_item_role_cannot_gain_myr_support():
    block = ParserOcrBlock(0, 0, "ITEM MYR 12.30", 0, 0, engine_block_index=0)
    assert explicit_item_line_groups((block,), currency="MYR") == ()


def test_historical_ai_policy_cannot_start_inheriting_myr_for_bare_amounts():
    binding = TrustedBinding(*(f"synthetic-{i}" for i in range(7)))
    provider = SimpleNamespace(
        binding=ProviderBinding(
            "synthetic-provider",
            "synthetic-model",
            "synthetic-adapter",
            "synthetic-endpoint",
            "synthetic-key",
        )
    )
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(MoneyValidationError, match="Unsupported currency 'MYR'"):
            AiInvocationService(
                connection=conn,
                binding=binding,
                source_verifier=None,
                interpretation_input_verifier=None,
                provider=provider,
                clock=lambda: 1,
                interpretation_policy=InterpretationPolicy("UTC", "MYR"),
            )


def test_historical_independent_correction_rejects_myr_before_any_write(
    migrated_temp_db_connection,
):
    conn = migrated_temp_db_connection
    posting, posted, authority = independent_corrections._posted_text(conn, "myr-policy")
    service = independent_corrections._correction_service(posting, authority=authority)
    before = tuple(conn.iterdump())
    with pytest.raises(MoneyValidationError, match="Unsupported currency 'MYR'"):
        service.prepare_correction(
            posted.transaction_public_id, {"amount": "18.75", "currency": "MYR"}, "Correct currency"
        )
    assert tuple(conn.iterdump()) == before
    plan = service.prepare_correction(
        posted.transaction_public_id, {"amount": "18.75", "currency": "USD"}, "Correct currency"
    )
    assert plan.projection["after"]["currency"] == "USD"


def test_historical_coupled_correction_rejects_myr_before_any_write():
    conn, service, _ = coupled_corrections._fixture()
    try:
        before = tuple(conn.iterdump())
        with pytest.raises(CorrectionError, match="Invalid currency"):
            service.preview("txn-1", {"amount": "18.75", "currency": "MYR"}, "Correct currency")
        assert tuple(conn.iterdump()) == before
        plan = service.preview("txn-1", {"amount": "18.75", "currency": "USD"}, "Correct currency")
        assert plan.after.currency == "USD"
    finally:
        conn.close()


def test_historical_native_lifecycle_rejects_myr_before_any_financial_write(live):
    service, port, _ = live
    raw = "2026-10-24 Cafe MYR 12.00"
    port.sources["source1"] = LifecycleSource(
        "source1",
        "user",
        "chat",
        "message-1",
        raw,
        hashlib.sha256(raw.encode()).hexdigest(),
        1792800000,
    )
    port.raw = lifecycle_response(
        raw,
        fields={
            "amount": "12.00",
            "currency": "MYR",
            "merchant": "Cafe",
            "transaction_date": "2026-10-24",
        },
    )
    source = service.capture("source1", "capture1")
    tables = ("l2_proposals", "l2_reviews", "l2_decisions", "l2_facts", "l2_events")
    with lifecycle_read_operation(service.owner) as conn:
        before = tuple(
            conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables
        )
    with pytest.raises(LifecycleError, match="Provider proposal is not complete"):
        service.propose(source.source_id, "myr-observation", 0, "myr-propose")
    with lifecycle_read_operation(service.owner) as conn:
        after = tuple(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables)
    assert after == before


def test_historical_native_and_legacy_import_domain_guard_keeps_eight_currencies():
    # The existing issued-export importer and fact/audit readers use this same
    # domain guard. A plain dict here grants no issued-export provenance.
    with pytest.raises(MoneyValidationError, match="Unsupported currency 'MYR'"):
        native_expense_money({"amount": "12.30", "currency": "MYR"})
    original = {"amount": "12.30", "currency": "SGD"}
    assert native_expense_money(original) == original
