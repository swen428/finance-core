"""S1C-C managed AI fallback and compatibility-receipt Bridge proofs."""

from __future__ import annotations

import pytest

from finance_core.openclaw_staging_bridge import commands, envelope, errors
from tests import test_ai_model_compatibility_receipts_v2 as compatibility_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests.s1cc_managed_test_support import (
    assert_exclusive_gate_released,
    fallback_response_arguments,
    read_count,
    request,
    run,
    seed_ai_source,
)

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)

_VERSION_COMMANDS = {
    "v1": {
        "prepare": envelope.COMMAND_PREPARE_AI_FALLBACK,
        "claim": envelope.COMMAND_CLAIM_AI_FALLBACK_INVOCATION,
        "record": envelope.COMMAND_RECORD_AI_FALLBACK_RESULT,
        "prepare_key": commands.canonical_prepare_ai_fallback_key,
        "claim_key": commands.canonical_claim_ai_fallback_key,
        "record_key": commands.canonical_record_ai_fallback_key,
    },
    "v2": {
        "prepare": envelope.COMMAND_PREPARE_AI_FALLBACK_V2,
        "claim": envelope.COMMAND_CLAIM_AI_FALLBACK_INVOCATION_V2,
        "record": envelope.COMMAND_RECORD_AI_FALLBACK_RESULT_V2,
        "prepare_key": commands.canonical_prepare_ai_fallback_v2_key,
        "claim_key": commands.canonical_claim_ai_fallback_v2_key,
        "record_key": commands.canonical_record_ai_fallback_v2_key,
    },
}


def _register_fixed_receipt(
    workspace: s1ca.ManagedBridgeWorkspace,
) -> tuple[dict[str, object], dict[str, object]]:
    projection = compatibility_tests._projection()
    outcomes = compatibility_tests._outcomes()
    expected_cases = {case["case_id"] for case in compatibility_tests._fixture_cases()}
    assert {outcome["case_id"] for outcome in outcomes} == expected_cases
    payload = request(
        workspace,
        envelope.COMMAND_REGISTER_AI_MODEL_COMPATIBILITY_RECEIPT_V2,
        {"config_projection": projection, "harness_outcomes": outcomes},
        idempotency_key=commands.canonical_register_ai_model_receipt_v2_key(projection),
    )
    first = run(workspace, payload)
    replay = run(workspace, payload)
    assert first.exit_code == errors.EXIT_OK, first.response
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert first.response["result"]["idempotent_replay"] is False
    assert replay.response["result"]["idempotent_replay"] is True
    assert (
        first.response["result"]["receipt_public_id"]
        == replay.response["result"]["receipt_public_id"]
    )
    assert read_count(workspace, "ai_model_compatibility_receipts") == 1
    return projection, first.response["result"]


def _fallback_args(
    workspace: s1ca.ManagedBridgeWorkspace,
    version: str,
    action: str,
    identity: str,
    *,
    source_kind: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    version_commands = _VERSION_COMMANDS[version]
    key_builder = version_commands[f"{action}_key"]
    assert callable(key_builder)
    return request(
        workspace,
        str(version_commands[action]),
        {
            "attempt_public_id" if action != "prepare" else "intake_public_id": identity,
            **(extra or {}),
        },
        idempotency_key=key_builder(identity),
    )


def test_fixed_four_case_compatibility_receipt_registers_and_replays(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    _register_fixed_receipt(managed_workspace)
    assert read_count(managed_workspace, "ai_model_compatibility_receipts") == 1


@pytest.mark.parametrize("version", ("v1", "v2"))
@pytest.mark.parametrize("source_kind", ("text", "receipt_ocr"))
def test_managed_public_prepare_claim_supplied_result_and_replay(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    source_kind: str,
) -> None:
    projection: dict[str, object] | None = None
    if version == "v2":
        projection, _receipt = _register_fixed_receipt(managed_workspace)

    _proposal_id, intake_id = seed_ai_source(managed_workspace, source_kind, monkeypatch)
    prepare_extra = {"config_projection": projection} if projection is not None else {}
    prepared = run(
        managed_workspace,
        _fallback_args(
            managed_workspace,
            version,
            "prepare",
            intake_id,
            source_kind=source_kind,
            extra=prepare_extra,
        ),
    )
    prepare_replay = run(
        managed_workspace,
        _fallback_args(
            managed_workspace,
            version,
            "prepare",
            intake_id,
            source_kind=source_kind,
            extra=prepare_extra,
        ),
    )
    assert prepared.exit_code == errors.EXIT_OK, prepared.response
    assert prepare_replay.exit_code == errors.EXIT_OK, prepare_replay.response
    prepared_result = prepared.response["result"]
    attempt_id = str(prepared_result["attempt_public_id"])
    assert prepared_result["claim_disposition"] == "claim_once"
    assert prepare_replay.response["result"]["attempt_public_id"] == attempt_id
    assert prepare_replay.response["result"]["claim_disposition"] == "do_not_claim"
    assert read_count(managed_workspace, "ai_fallback_attempts") == 1

    if version == "v2":
        # Existing domain evidence establishes that a v2 attempt cannot use v1 claim.
        wrong_version = run(
            managed_workspace,
            _fallback_args(
                managed_workspace,
                "v1",
                "claim",
                attempt_id,
                source_kind=source_kind,
            ),
        )
        assert wrong_version.exit_code == errors.EXIT_AUTHORITY_REFUSED
        assert wrong_version.response["error"]["code"] == errors.AI_FALLBACK_CONFLICT
        assert read_count(managed_workspace, "ai_fallback_invocation_claims") == 0

    claim_request = _fallback_args(
        managed_workspace,
        version,
        "claim",
        attempt_id,
        source_kind=source_kind,
    )
    claimed = run(managed_workspace, claim_request)
    claim_replay = run(managed_workspace, claim_request)
    assert claimed.exit_code == errors.EXIT_OK, claimed.response
    assert claim_replay.exit_code == errors.EXIT_OK, claim_replay.response
    claim = claimed.response["result"]
    assert claim["invocation_disposition"] == "invoke_once"
    assert claim_replay.response["result"]["invocation_disposition"] == "do_not_invoke"
    assert read_count(managed_workspace, "ai_fallback_invocation_claims") == 1

    # This is the synthetic provider-wait interval.  The managed gate must be
    # available after claim has returned and before the later result request.
    assert_exclusive_gate_released(managed_workspace)

    arguments = fallback_response_arguments(
        claim,
        version=version,
        source_kind=source_kind,
    )
    record_request = request(
        managed_workspace,
        str(_VERSION_COMMANDS[version]["record"]),
        {
            "attempt_public_id": attempt_id,
            "transport_outcome": "response_received",
            **arguments,
        },
        idempotency_key=_VERSION_COMMANDS[version]["record_key"](attempt_id),
    )
    recorded = run(managed_workspace, record_request)
    result_replay = run(managed_workspace, record_request)
    assert recorded.exit_code == errors.EXIT_OK, recorded.response
    assert result_replay.exit_code == errors.EXIT_OK, result_replay.response
    assert recorded.response["result"]["result_status"] == "proposal_created"
    assert recorded.response["result"]["proposal_public_id"]
    assert result_replay.response["idempotent_replay"] is True
    assert read_count(managed_workspace, "ai_fallback_results") == 1
    assert read_count(managed_workspace, "ai_fallback_proposal_links") == 1
    assert read_count(managed_workspace, "parser_proposal_confirmations") == 0
    assert read_count(managed_workspace, "transactions") == 0


def test_managed_v2_missing_receipt_refusal_keeps_one_terminal_denial(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal_id, intake_id = seed_ai_source(managed_workspace, "text", monkeypatch)
    projection = compatibility_tests._projection()
    payload = _fallback_args(
        managed_workspace,
        "v2",
        "prepare",
        intake_id,
        source_kind="text",
        extra={"config_projection": projection},
    )
    first = run(managed_workspace, payload)
    replay = run(managed_workspace, payload)
    first_session = managed_workspace.sessions[-2]
    replay_session = managed_workspace.sessions[-1]
    assert first.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert first.response["error"]["code"] == errors.AI_MODEL_CONFIG_NOT_ACCEPTED
    assert replay.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert replay.response["error"]["code"] == errors.AI_MODEL_CONFIG_NOT_ACCEPTED
    assert read_count(managed_workspace, "ai_model_admission_decisions") == 1
    assert read_count(managed_workspace, "ai_fallback_attempts") == 0
    assert read_count(managed_workspace, "ai_fallback_invocation_claims") == 0
    assert first_session.total_changes > 0
    assert replay_session.total_changes == 0


def test_managed_result_refuses_another_claims_source_and_unclaimed_attempt(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_proposal, text_intake = seed_ai_source(managed_workspace, "text", monkeypatch)
    image_proposal, image_intake = seed_ai_source(managed_workspace, "receipt_ocr", monkeypatch)

    prepared_attempts: dict[str, str] = {}
    for proposal_id, intake_id in (
        (text_proposal, text_intake),
        (image_proposal, image_intake),
    ):
        prepared = run(
            managed_workspace,
            _fallback_args(
                managed_workspace,
                "v1",
                "prepare",
                intake_id,
                source_kind="text",
            ),
        )
        assert prepared.exit_code == errors.EXIT_OK, prepared.response
        prepared_attempts[proposal_id] = str(prepared.response["result"]["attempt_public_id"])

    image_attempt = prepared_attempts[image_proposal]
    image_claimed = run(
        managed_workspace,
        _fallback_args(
            managed_workspace,
            "v1",
            "claim",
            image_attempt,
            source_kind="receipt_ocr",
        ),
    )
    assert image_claimed.exit_code == errors.EXIT_OK, image_claimed.response
    image_args = fallback_response_arguments(
        image_claimed.response["result"],
        version="v1",
        source_kind="receipt_ocr",
    )

    text_attempt = prepared_attempts[text_proposal]
    wrong_without_claim = run(
        managed_workspace,
        request(
            managed_workspace,
            envelope.COMMAND_RECORD_AI_FALLBACK_RESULT,
            {
                "attempt_public_id": text_attempt,
                "transport_outcome": "response_received",
                **image_args,
            },
            idempotency_key=commands.canonical_record_ai_fallback_key(text_attempt),
        ),
    )
    assert wrong_without_claim.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert wrong_without_claim.response["error"]["code"] == errors.AI_FALLBACK_CONFLICT
    assert read_count(managed_workspace, "ai_fallback_results") == 0
    assert read_count(managed_workspace, "ai_fallback_proposal_links") == 0
    assert managed_workspace.sessions[-1].total_changes == 0

    text_claimed = run(
        managed_workspace,
        _fallback_args(
            managed_workspace,
            "v1",
            "claim",
            text_attempt,
            source_kind="text",
        ),
    )
    assert text_claimed.exit_code == errors.EXIT_OK, text_claimed.response
    wrong_source = run(
        managed_workspace,
        request(
            managed_workspace,
            envelope.COMMAND_RECORD_AI_FALLBACK_RESULT,
            {
                "attempt_public_id": text_attempt,
                "transport_outcome": "response_received",
                **image_args,
            },
            idempotency_key=commands.canonical_record_ai_fallback_key(text_attempt),
        ),
    )
    mismatch_session = managed_workspace.sessions[-1]
    assert wrong_source.exit_code == errors.EXIT_OK, wrong_source.response
    terminal = wrong_source.response["result"]
    assert terminal["result_status"] == "response_refused"
    assert terminal["non_child_reason"] == "validation_refused"
    assert terminal["proposal_public_id"] is None
    assert mismatch_session.total_changes > 0

    terminal_replay = run(
        managed_workspace,
        request(
            managed_workspace,
            envelope.COMMAND_RECORD_AI_FALLBACK_RESULT,
            {
                "attempt_public_id": text_attempt,
                "transport_outcome": "response_received",
                **image_args,
            },
            idempotency_key=commands.canonical_record_ai_fallback_key(text_attempt),
        ),
    )
    replay_session = managed_workspace.sessions[-1]
    assert terminal_replay.exit_code == errors.EXIT_OK, terminal_replay.response
    assert terminal_replay.response["idempotent_replay"] is True
    assert terminal_replay.response["result"] == terminal
    assert replay_session.total_changes == 0

    assert read_count(managed_workspace, "ai_fallback_results") == 1
    assert read_count(managed_workspace, "ai_fallback_proposal_links") == 0
    assert read_count(managed_workspace, "parser_outputs") == 2
    assert read_count(managed_workspace, "parser_proposal_confirmations") == 0
    assert read_count(managed_workspace, "transactions") == 0
