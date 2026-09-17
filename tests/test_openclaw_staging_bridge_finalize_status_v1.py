"""S4 contract tests: finalize, authorize_finalization, apply_fact_set.

Covers the merged S4 contract (D1b human-authored out-of-envelope fact sets
via the guarded IAF boundary, D2B snapshot-bound human authorization) for
the OpenClaw staging bridge: text-path finalize through the existing
confirmed-proposal conversion boundary, receipt-path D1b guard, D2B state
sequence (prepare -> SNAPSHOT_AUTHORIZATION_REQUIRED -> authorize ->
finalize exactly once), stale/wrong-actor refusals, idempotent replays,
crash-recovery continuation, concurrency, and envelope hygiene.  All data
is temporary, synthetic, and staging-authorised; nothing here touches the
live database, credentials, or the network.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.calculation.authoritative_snapshot import load_snapshot_for_authoritative_use
from finance_core.calculators.receipt_calculator_readiness import (
    REASON_NO_AUTHORITATIVE_ITEM_FACTS,
)
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import identity, ocr_boundary
from finance_core.parser_proposals.receipt_facts_conversion import derive_receipt_public_id
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.participants import bootstrap_participants
from tests.test_openclaw_staging_bridge_decisions_v1 import (
    decision_arguments,
    decision_key,
    get_review,
    setup_text_proposal,
)
from tests.test_receipt_ocr_proposal_ingestion_v1 import _sgd_blocks

OPERATOR = "person_owner"
SELF_PARTICIPANT = "ptcp_owner"
BRIDGE_CHANNEL = "openclaw_staging_bridge"


# ---------------------------------------------------------------------------
# Fixtures and flow helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def bootstrap_self_participant(workspace: support.BridgeWorkspace) -> None:
    conn = support.open_database(workspace)
    try:
        manifest = parse_runner_manifest(support.make_manifest_bytes())
        bootstrap_participants(conn, manifest)
        conn.commit()
    finally:
        conn.close()


def confirm_via_bridge(workspace: support.BridgeWorkspace, proposal_public_id: str) -> dict:
    review = get_review(workspace, proposal_public_id)
    arguments = decision_arguments(workspace, review, "confirm")
    outcome = support.run_cli(
        support.make_request("confirm", arguments, idempotency_key=decision_key("confirm", review))
    )
    assert outcome.exit_code == bridge_errors.EXIT_OK
    return outcome.response["result"]


def set_transaction_date_via_bridge(
    workspace: support.BridgeWorkspace, proposal_public_id: str
) -> dict:
    review = get_review(workspace, proposal_public_id)
    arguments = decision_arguments(workspace, review, "edit")
    arguments["field_updates"] = {"transaction_date": "2026-06-01"}
    outcome = support.run_cli(
        support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
    )
    assert outcome.exit_code == bridge_errors.EXIT_OK
    return outcome.response["result"]


def setup_convertible_text_proposal(workspace: support.BridgeWorkspace) -> dict:
    """Confirmed text proposal with all conversion-required fields."""
    capture = setup_text_proposal(workspace, "Coffee SGD 6.40 at Starbucks", "finalize-text")
    set_transaction_date_via_bridge(workspace, capture["proposal_public_id"])
    confirm_via_bridge(workspace, capture["proposal_public_id"])
    return get_review(workspace, capture["proposal_public_id"])


def setup_confirmed_receipt_proposal(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Confirmed receipt proposal parsed from deterministic OCR blocks."""
    support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    intake_public_id = capture.response["result"]["intake_public_id"]
    engine = support.FakeOcrEngine(blocks=_sgd_blocks())
    monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
    propose = support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
            idempotency_key=support.canonical_propose_key(intake_public_id),
        )
    )
    assert propose.exit_code == bridge_errors.EXIT_OK
    proposal_public_id = propose.response["result"]["proposal_public_id"]
    confirm_via_bridge(workspace, proposal_public_id)
    return get_review(workspace, proposal_public_id)


def finalize_arguments(workspace: support.BridgeWorkspace, review: dict) -> dict:
    return {
        "workspace_path": str(workspace.workspace_path),
        "proposal_public_id": review["proposal_public_id"],
        "operator_actor_id": OPERATOR,
        "proposal_version": review["proposal_version"],
        "content_hash": review["effective_content_hash"],
    }


def run_finalize(
    workspace: support.BridgeWorkspace, review: dict, *, key_suffix: str = ""
) -> support.CliOutcome:
    proposal_public_id = review["proposal_public_id"]
    return support.run_cli(
        support.make_request(
            "finalize",
            finalize_arguments(workspace, review),
            idempotency_key=f"bridge-finalize:{proposal_public_id}{key_suffix}",
        )
    )


def run_prepare_receipt_completion(
    workspace: support.BridgeWorkspace, review: dict
) -> support.CliOutcome:
    proposal_public_id = review["proposal_public_id"]
    return support.run_cli(
        support.make_request(
            "prepare_receipt_completion",
            finalize_arguments(workspace, review),
            idempotency_key=f"bridge-prepare-receipt:{proposal_public_id}",
        )
    )


def run_snapshot_review(workspace: support.BridgeWorkspace, review: dict) -> support.CliOutcome:
    proposal_public_id = review["proposal_public_id"]
    return support.run_cli(
        support.make_request(
            "get_finalization_snapshot_review",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
                "operator_actor_id": OPERATOR,
            },
            idempotency_key=f"bridge-finalization-snapshot-review:{proposal_public_id}",
        )
    )


def receipt_identities(review: dict) -> tuple[str, str]:
    command_id = identity.receipt_conversion_command_public_id(
        review["proposal_public_id"], review["effective_content_hash"]
    )
    return command_id, derive_receipt_public_id(command_id)


def write_command_file(workspace: support.BridgeWorkspace, filename: str, payload: dict) -> Path:
    commands_dir = workspace.workspace_path / "commands"
    commands_dir.mkdir(mode=0o700, exist_ok=True)
    path = commands_dir / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build_iaf_command(
    *,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
    actor: str = OPERATOR,
    actor_type: str = "human",
    channel: str = BRIDGE_CHANNEL,
    participants: list[dict[str, Any]] | None = None,
    adjustments: list[dict[str, Any]] | None = None,
) -> dict:
    return {
        "command_public_id": "riaf_bridge_test_0001",
        "receipt_public_id": receipt_public_id,
        "expected_conversion_command_public_id": conversion_command_public_id,
        "expected_conversion_result_hash": conversion_result_hash,
        "expected_current_fact_set": "none",
        "items": [
            {"line_number": 1, "item_name": "Total", "line_amount": "12.34", "currency": "SGD"}
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": participants
                if participants is not None
                else [
                    {
                        "participant_public_id": SELF_PARTICIPANT,
                        "share_amount": "12.34",
                        "currency": "SGD",
                    }
                ],
            }
        ],
        "adjustments": adjustments if adjustments is not None else [],
        "authenticated_actor_id": actor,
        "actor_type": actor_type,
        "channel": channel,
        "schema_version": "v1",
    }


def run_apply_fact_set(
    workspace: support.BridgeWorkspace, review: dict, command_filename: str
) -> support.CliOutcome:
    proposal_public_id = review["proposal_public_id"]
    return support.run_cli(
        support.make_request(
            "apply_fact_set",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
                "operator_actor_id": OPERATOR,
                "command_filename": command_filename,
            },
            idempotency_key=f"bridge-apply-fact-set:{proposal_public_id}",
        )
    )


def run_authorize(
    workspace: support.BridgeWorkspace, review: dict, expected_hash: str
) -> support.CliOutcome:
    proposal_public_id = review["proposal_public_id"]
    return support.run_cli(
        support.make_request(
            "authorize_finalization",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
                "operator_actor_id": OPERATOR,
                "expected_calculation_snapshot_hash": expected_hash,
            },
            idempotency_key=f"bridge-authorize-finalization:{proposal_public_id}",
        )
    )


def get_status(
    workspace: support.BridgeWorkspace,
    *,
    proposal_public_id: str | None = None,
    intake_public_id: str | None = None,
) -> dict:
    arguments: dict[str, Any] = {"workspace_path": str(workspace.workspace_path)}
    if proposal_public_id is not None:
        arguments["proposal_public_id"] = proposal_public_id
    if intake_public_id is not None:
        arguments["intake_public_id"] = intake_public_id
    outcome = support.run_cli(support.make_request("get_status", arguments))
    assert outcome.exit_code == bridge_errors.EXIT_OK
    return outcome.response["result"]


def apply_human_fact_set(workspace: support.BridgeWorkspace, review: dict) -> support.CliOutcome:
    """Run the D1b hand-off: refused finalize -> author -> apply_fact_set."""
    refused = run_finalize(workspace, review)
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    error = refused.response["error"]
    assert error["code"] == bridge_errors.FINALIZATION_REFUSED
    assert error["details"]["reason"] == "no_authoritative_item_facts"
    command_id, receipt_id = receipt_identities(review)
    write_command_file(
        workspace,
        "fact-set.json",
        build_iaf_command(
            receipt_public_id=receipt_id,
            conversion_command_public_id=command_id,
            conversion_result_hash=error["details"]["conversion_result_hash"],
        ),
    )
    return run_apply_fact_set(workspace, review, "fact-set.json")


def prepare_receipt_with_applied_fact_set(
    workspace: support.BridgeWorkspace, review: dict
) -> support.CliOutcome:
    prepared = run_prepare_receipt_completion(workspace, review)
    assert prepared.exit_code == bridge_errors.EXIT_OK
    command_id, receipt_id = receipt_identities(review)
    write_command_file(
        workspace,
        "fact-set.json",
        build_iaf_command(
            receipt_public_id=receipt_id,
            conversion_command_public_id=command_id,
            conversion_result_hash=prepared.response["result"]["conversion_result_hash"],
        ),
    )
    applied = run_apply_fact_set(workspace, review, "fact-set.json")
    assert applied.exit_code == bridge_errors.EXIT_OK
    return applied


def first_snapshot_refusal(
    workspace: support.BridgeWorkspace,
    review: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> support.CliOutcome:
    """Drive finalize through D1b into the D2B authorization gate."""
    apply = apply_human_fact_set(workspace, review)
    assert apply.exit_code == bridge_errors.EXIT_OK
    outcome = run_finalize(workspace, review)
    assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    error = outcome.response["error"]
    assert error["code"] == bridge_errors.SNAPSHOT_AUTHORIZATION_REQUIRED
    return outcome


def authorize_and_finalize(
    workspace: support.BridgeWorkspace, review: dict, expected_hash: str
) -> support.CliOutcome:
    authorize = run_authorize(workspace, review, expected_hash)
    assert authorize.exit_code == bridge_errors.EXIT_OK
    return run_finalize(workspace, review)


def table_counts(workspace: support.BridgeWorkspace, tables: tuple[str, ...]) -> dict[str, int]:
    conn = support.open_database(workspace)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Envelope and usage contract
# ---------------------------------------------------------------------------


class TestEnvelopeContract:
    def test_finalize_rejects_unknown_monetary_argument(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        arguments = finalize_arguments(workspace, review)
        arguments["amount"] = "6.40"
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
        finally:
            conn.close()

    def test_finalize_requires_idempotency_key(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        outcome = support.run_cli(
            support.make_request("finalize", finalize_arguments(workspace, review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["error"]["code"] == bridge_errors.MISSING_IDEMPOTENCY_KEY

    def test_finalize_rejects_non_canonical_idempotency_key(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                finalize_arguments(workspace, review),
                idempotency_key="my-own-key",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    @pytest.mark.parametrize("receipt_only", ["true", 1, None])
    def test_finalize_refuses_non_boolean_receipt_only(self, workspace, receipt_only) -> None:
        review = setup_convertible_text_proposal(workspace)
        arguments = dict(finalize_arguments(workspace, review), receipt_only=receipt_only)
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
        assert table_counts(workspace, ("transactions",)) == {"transactions": 0}

    def test_authorize_finalization_rejects_missing_expected_hash(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        arguments = {
            "workspace_path": str(workspace.workspace_path),
            "proposal_public_id": review["proposal_public_id"],
            "operator_actor_id": OPERATOR,
        }
        outcome = support.run_cli(
            support.make_request(
                "authorize_finalization",
                arguments,
                idempotency_key=f"bridge-authorize-finalization:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    @pytest.mark.parametrize(
        "filename",
        ["../evil.json", "nested/dir.json", ".hidden.json", "..", "name\x00.json"],
    )
    def test_apply_fact_set_rejects_unsafe_filename(self, workspace, monkeypatch, filename) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        outcome = run_apply_fact_set(workspace, review, filename)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED


# ---------------------------------------------------------------------------
# Text-path finalize
# ---------------------------------------------------------------------------


class TestTextFinalize:
    def test_finalize_creates_canonical_transaction(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        outcome = run_finalize(workspace, review)
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["path"] == "text_expense"
        assert result["proposal_public_id"] == review["proposal_public_id"]
        assert result["final_transaction_created"] is True
        assert result["transaction_public_id"].startswith("txn_")
        assert result["confirmation_public_id"]
        assert result["content_hash"] == review["effective_content_hash"]
        # Envelope hygiene: no monetary material in the success response.
        assert "amount" not in result and "currency" not in result

    def test_receipt_only_finalize_refuses_text_before_transaction(self, workspace) -> None:
        """S5d's direct receipt route must not fall through to text finalization."""
        review = setup_convertible_text_proposal(workspace)
        arguments = dict(finalize_arguments(workspace, review), receipt_only=True)

        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )

        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        error = outcome.response["error"]
        assert error["code"] == bridge_errors.FINALIZATION_REFUSED
        assert error["details"]["reason"] == "unsupported_path"
        assert table_counts(workspace, ("transactions", "parser_proposal_conversion_audit")) == {
            "transactions": 0,
            "parser_proposal_conversion_audit": 0,
        }

    def test_identical_finalize_replays_idempotently(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        first = run_finalize(workspace, review)
        assert first.exit_code == bridge_errors.EXIT_OK
        before = table_counts(workspace, ("transactions", "parser_proposal_conversion_audit"))
        second = run_finalize(workspace, review)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["result"] == first.response["result"]
        after = table_counts(workspace, ("transactions", "parser_proposal_conversion_audit"))
        assert after == before

    def test_finalize_rejects_stale_version_and_hash(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        stale_version = dict(finalize_arguments(workspace, review), proposal_version=99)
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                stale_version,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_VERSION
        stale_hash = dict(finalize_arguments(workspace, review), content_hash="f" * 64)
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                stale_hash,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_CONTENT_HASH

    def test_finalize_rejects_wrong_operator_actor(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        arguments = finalize_arguments(workspace, review)
        arguments["operator_actor_id"] = "person_other"
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ACTOR_MISMATCH

    def test_finalize_rejects_unconfirmed_proposal(self, workspace) -> None:
        capture = setup_text_proposal(workspace, "lunch 12.50", "finalize-unconfirmed")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = run_finalize(workspace, review)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        error = outcome.response["error"]
        assert error["code"] == bridge_errors.FINALIZATION_REFUSED
        assert error["details"]["reason"] == "not_confirmed"

    def test_get_status_reports_finalized_text_proposal(self, workspace) -> None:
        review = setup_convertible_text_proposal(workspace)
        assert (
            get_status(workspace, proposal_public_id=review["proposal_public_id"])[
                "finalization_state"
            ]
            == "confirmed_incomplete"
        )
        finalize = run_finalize(workspace, review)
        assert finalize.exit_code == bridge_errors.EXIT_OK
        status = get_status(workspace, proposal_public_id=review["proposal_public_id"])
        assert status["finalization_state"] == "finalized"
        assert status["final_transaction_created"] is True
        assert (
            status["transaction_public_id"] == finalize.response["result"]["transaction_public_id"]
        )


# ---------------------------------------------------------------------------
# Receipt path: D1b guard and D2B authorization sequence
# ---------------------------------------------------------------------------


class TestReceiptFinalizeD1bD2b:
    def test_prepare_receipt_completion_converts_once_without_snapshot_or_authorization(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)

        first = run_prepare_receipt_completion(workspace, review)

        assert first.exit_code == bridge_errors.EXIT_OK
        assert first.response["idempotent_replay"] is False
        result = first.response["result"]
        command_id, receipt_id = receipt_identities(review)
        assert result == {
            "identity_kind": "prepare_receipt_completion",
            "proposal_public_id": review["proposal_public_id"],
            "receipt_public_id": receipt_id,
            "conversion_command_public_id": command_id,
            "conversion_result_hash": result["conversion_result_hash"],
            "content_hash": review["effective_content_hash"],
        }
        assert len(result["conversion_result_hash"]) == 64
        assert (
            get_status(workspace, proposal_public_id=review["proposal_public_id"])[
                "finalization_state"
            ]
            == "confirmed_incomplete"
        )
        guarded_tables = (
            "receipt_proposal_conversions",
            "authoritative_calculation_snapshots",
            "receipt_finalization_authorizations",
            "transactions",
        )
        assert table_counts(workspace, guarded_tables) == {
            "receipt_proposal_conversions": 1,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "transactions": 0,
        }
        replay = run_prepare_receipt_completion(workspace, review)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True
        assert replay.response["result"] == result
        assert table_counts(workspace, guarded_tables) == {
            "receipt_proposal_conversions": 1,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "transactions": 0,
        }

    def test_snapshot_review_returns_verified_authoritative_output_and_hash(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        prepare_receipt_with_applied_fact_set(workspace, review)

        outcome = run_snapshot_review(workspace, review)

        assert outcome.exit_code == bridge_errors.EXIT_OK
        assert outcome.response["idempotent_replay"] is False
        result = outcome.response["result"]
        _command_id, receipt_id = receipt_identities(review)
        assert result["identity_kind"] == "finalization_snapshot_review"
        assert result["receipt_public_id"] == receipt_id
        assert result["currency"] == "SGD"
        assert result["total_paid"] == "12.34"
        assert result["total_to_collect"] == "0.00"
        assert result["participant_shares"] == {SELF_PARTICIPANT: "12.34"}
        assert result["settlement_obligations"] == []
        assert result["calculation_snapshot_id"].startswith("snap_")
        assert len(result["calculation_snapshot_hash"]) == 64
        conn = support.open_database(workspace)
        try:
            snapshot = load_snapshot_for_authoritative_use(conn, result["calculation_snapshot_id"])
            snapshot.verify()
            assert snapshot.combined_snapshot_hash == result["calculation_snapshot_hash"]
        finally:
            conn.close()
        assert (
            get_status(workspace, proposal_public_id=review["proposal_public_id"])[
                "finalization_state"
            ]
            == "prepared_pending_authorization"
        )
        assert table_counts(
            workspace,
            (
                "authoritative_calculation_snapshots",
                "receipt_finalization_authorizations",
                "transactions",
            ),
        ) == {
            "authoritative_calculation_snapshots": 1,
            "receipt_finalization_authorizations": 0,
            "transactions": 0,
        }

    def test_snapshot_review_never_finalizes_an_authorized_receipt(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        prepare_receipt_with_applied_fact_set(workspace, review)
        first = run_snapshot_review(workspace, review)
        assert first.exit_code == bridge_errors.EXIT_OK
        expected_hash = first.response["result"]["calculation_snapshot_hash"]
        authorized = run_authorize(workspace, review, expected_hash)
        assert authorized.exit_code == bridge_errors.EXIT_OK

        replay = run_snapshot_review(workspace, review)

        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True
        assert replay.response["result"] == first.response["result"]
        assert table_counts(
            workspace,
            ("receipt_finalization_authorizations", "transactions"),
        ) == {"receipt_finalization_authorizations": 1, "transactions": 0}

    def test_snapshot_review_replay_reports_durable_snapshot_reuse(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        prepare_receipt_with_applied_fact_set(workspace, review)

        first = run_snapshot_review(workspace, review)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert first.response["idempotent_replay"] is False
        replay = run_snapshot_review(workspace, review)

        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True
        assert replay.response["result"] == first.response["result"]
        assert table_counts(
            workspace,
            (
                "authoritative_calculation_snapshots",
                "receipt_finalization_authorizations",
                "transactions",
            ),
        ) == {
            "authoritative_calculation_snapshots": 1,
            "receipt_finalization_authorizations": 0,
            "transactions": 0,
        }

    def test_finalize_requires_participant_bootstrap(self, workspace, monkeypatch) -> None:
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        outcome = run_finalize(workspace, review)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        error = outcome.response["error"]
        assert error["code"] == bridge_errors.FINALIZATION_REFUSED
        assert error["details"]["reason"] == "participants_not_bootstrapped"

    def test_finalize_without_fact_set_is_refused_with_authoring_materials(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        outcome = run_finalize(workspace, review)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        error = outcome.response["error"]
        assert error["code"] == bridge_errors.FINALIZATION_REFUSED
        assert error["details"]["reason"] == "no_authoritative_item_facts"
        command_id, receipt_id = receipt_identities(review)
        assert error["details"]["conversion_command_public_id"] == command_id
        assert error["details"]["receipt_public_id"] == receipt_id
        assert error["details"]["conversion_result_hash"]
        # D1b authoring material is bounded: no workspace paths or key material.
        rendered = json.dumps(outcome.response)
        support.assert_no_sensitive_material(rendered, workspace)
        # Conversion itself did persist (idempotent replay foundation).
        conn = support.open_database(workspace)
        try:
            conversions = int(
                conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0]
            )
            assert conversions == 1
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
        finally:
            conn.close()
        # get_status reconstructs the incomplete state from durable truth,
        # exposing the readiness reasons after conversion exists.
        status = get_status(workspace, proposal_public_id=review["proposal_public_id"])
        assert status["finalization_state"] == "confirmed_incomplete"
        assert REASON_NO_AUTHORITATIVE_ITEM_FACTS in status["readiness_reasons"]

    def test_apply_fact_set_persists_human_fact_set_and_replays(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = run_finalize(workspace, review)
        conversion_result_hash = refused.response["error"]["details"]["conversion_result_hash"]
        command_id, receipt_id = receipt_identities(review)
        write_command_file(
            workspace,
            "fact-set.json",
            build_iaf_command(
                receipt_public_id=receipt_id,
                conversion_command_public_id=command_id,
                conversion_result_hash=conversion_result_hash,
            ),
        )
        outcome = run_apply_fact_set(workspace, review, "fact-set.json")
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["fact_set_version"] == 1
        assert result["receipt_public_id"] == receipt_id
        assert result["item_count"] == 1 and result["allocation_count"] == 1
        before = table_counts(workspace, ("receipt_item_allocation_fact_sets",))
        replay = run_apply_fact_set(workspace, review, "fact-set.json")
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["result"] == result
        assert table_counts(workspace, ("receipt_item_allocation_fact_sets",)) == before

    def test_apply_fact_set_rejects_non_personal_allocation(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = run_finalize(workspace, review)
        conversion_result_hash = refused.response["error"]["details"]["conversion_result_hash"]
        command_id, receipt_id = receipt_identities(review)
        write_command_file(
            workspace,
            "foreign.json",
            build_iaf_command(
                receipt_public_id=receipt_id,
                conversion_command_public_id=command_id,
                conversion_result_hash=conversion_result_hash,
                participants=[
                    {
                        "participant_public_id": "ptcp_other",
                        "share_amount": "12.34",
                        "currency": "SGD",
                    }
                ],
            ),
        )
        outcome = run_apply_fact_set(workspace, review, "foreign.json")
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_apply_fact_set_rejects_wrong_actor_binding(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = run_finalize(workspace, review)
        conversion_result_hash = refused.response["error"]["details"]["conversion_result_hash"]
        command_id, receipt_id = receipt_identities(review)
        write_command_file(
            workspace,
            "wrong-actor.json",
            build_iaf_command(
                receipt_public_id=receipt_id,
                conversion_command_public_id=command_id,
                conversion_result_hash=conversion_result_hash,
                actor="person_other",
            ),
        )
        outcome = run_apply_fact_set(workspace, review, "wrong-actor.json")
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ACTOR_MISMATCH

    def test_apply_fact_set_rejects_missing_or_malformed_file(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        run_finalize(workspace, review)  # conversion lineage
        missing = run_apply_fact_set(workspace, review, "missing.json")
        assert missing.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert missing.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
        write_command_file(workspace, "malformed.json", {"not": "a-command"})
        malformed = run_apply_fact_set(workspace, review, "malformed.json")
        assert malformed.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert malformed.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_apply_fact_set_rejects_text_proposal(self, workspace) -> None:
        bootstrap_self_participant(workspace)
        review = setup_convertible_text_proposal(workspace)
        write_command_file(
            workspace,
            "ignored.json",
            build_iaf_command(
                receipt_public_id="rcpt_x",
                conversion_command_public_id="rpfc_bridge_x",
                conversion_result_hash="e" * 64,
            ),
        )
        outcome = run_apply_fact_set(workspace, review, "ignored.json")
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["details"]["reason"] == "unsupported_path"

    def test_finalize_without_authorization_returns_snapshot_review_materials(
        self, workspace, monkeypatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        error = refused.response["error"]
        details = error["details"]
        _command_id, receipt_id = receipt_identities(review)
        assert details["receipt_public_id"] == receipt_id
        assert details["calculation_snapshot_id"].startswith("snap_")
        assert len(details["calculation_snapshot_hash"]) == 64
        assert details["authorization_id"].startswith("authz_")
        rendered = json.dumps(refused.response)
        support.assert_no_sensitive_material(rendered, workspace)
        # No final facts, authorization row, or settlement created yet.
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
            assert (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM receipt_finalization_authorizations"
                    ).fetchone()[0]
                )
                == 0
            )
        finally:
            conn.close()

    def test_authorize_rejects_wrong_snapshot_hash(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        outcome = run_authorize(workspace, review, "ab" * 32)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_SNAPSHOT
        assert (
            outcome.response["error"]["details"]["calculation_snapshot_id"]
            == refused.response["error"]["details"]["calculation_snapshot_id"]
        )

    def test_authorize_rejects_text_proposal(self, workspace) -> None:
        bootstrap_self_participant(workspace)
        review = setup_convertible_text_proposal(workspace)
        outcome = run_authorize(workspace, review, "ab" * 32)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["details"]["reason"] == "unsupported_path"

    def test_authorize_rejects_wrong_operator(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        first_snapshot_refusal(workspace, review, monkeypatch)
        arguments = {
            "workspace_path": str(workspace.workspace_path),
            "proposal_public_id": review["proposal_public_id"],
            "operator_actor_id": "person_other",
            "expected_calculation_snapshot_hash": "ab" * 32,
        }
        outcome = support.run_cli(
            support.make_request(
                "authorize_finalization",
                arguments,
                idempotency_key=f"bridge-authorize-finalization:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ACTOR_MISMATCH

    def test_full_d2b_sequence_finalizes_exactly_once(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        finalize = authorize_and_finalize(workspace, review, expected_hash)
        assert finalize.exit_code == bridge_errors.EXIT_OK
        result = finalize.response["result"]
        assert result["path"] == "receipt"
        assert result["finalization_public_id"].startswith("fin_")
        assert result["transaction_public_id"]
        assert result["fact_set_public_id"]
        assert result["calculation_snapshot_hash"] == expected_hash
        assert (
            result["authorization_id"] == refused.response["error"]["details"]["authorization_id"]
        )
        assert "amount" not in result and "currency" not in result
        # Durable truth: exactly one transaction, one consumed authorization,
        # one finalized audit row.
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 1
            state_row = conn.execute(
                "SELECT authorization_state FROM receipt_finalization_authorizations"
            ).fetchone()
            assert str(state_row[0]) == "consumed"
            audit_row = conn.execute("SELECT status FROM receipt_finalization_audit").fetchone()
            assert str(audit_row[0]) == "finalized"
        finally:
            conn.close()

    def test_finalize_replay_after_success_is_idempotent(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        first = authorize_and_finalize(workspace, review, expected_hash)
        assert first.exit_code == bridge_errors.EXIT_OK
        guarded_tables = (
            "transactions",
            "receipt_finalization_audit",
            "receipt_finalization_authorizations",
            "authoritative_calculation_snapshots",
        )
        before = table_counts(workspace, guarded_tables)
        replay = run_finalize(workspace, review)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["result"] == first.response["result"]
        assert table_counts(workspace, guarded_tables) == before

    def test_crash_recovery_continues_from_durable_stage(self, workspace, monkeypatch) -> None:
        """A 'crashed' CLI is just a new process: state is rebuilt from truth."""
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        authorize = run_authorize(workspace, review, expected_hash)
        assert authorize.exit_code == bridge_errors.EXIT_OK
        # Simulate crash between authorize and finalize: the next finalize is
        # a fresh CLI invocation against durable truth and must complete.
        status = get_status(workspace, proposal_public_id=review["proposal_public_id"])
        assert status["finalization_state"] == "authorized_pending_finalization"
        finalize = run_finalize(workspace, review)
        assert finalize.exit_code == bridge_errors.EXIT_OK
        assert finalize.response["result"]["path"] == "receipt"

    def test_concurrent_finalize_finalizes_exactly_once(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        authorize = run_authorize(workspace, review, expected_hash)
        assert authorize.exit_code == bridge_errors.EXIT_OK

        outcomes: list[support.CliOutcome] = []
        lock = threading.Lock()

        def worker() -> None:
            outcome = run_finalize(workspace, review)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [o for o in outcomes if o.exit_code == bridge_errors.EXIT_OK]
        locked = [
            o
            for o in outcomes
            if o.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
            and o.response["error"]["code"] == bridge_errors.FINALIZATION_LOCKED
        ]
        assert len(successes) + len(locked) == len(outcomes)
        assert len(successes) >= 1
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 1
            finalized_rows = conn.execute(
                "SELECT COUNT(*) FROM receipt_finalization_audit "
                "WHERE status IN ('finalized', 'already_finalized')"
            ).fetchone()
            assert int(finalized_rows[0]) == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Finalization-aware get_status state reconstruction
# ---------------------------------------------------------------------------


class TestFinalizationStateReconstruction:
    def test_state_machine_reconstruction(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        proposal_public_id = review["proposal_public_id"]

        assert (
            get_status(workspace, proposal_public_id=proposal_public_id)["finalization_state"]
            == "confirmed_incomplete"
        )

        # Prepare runs inside finalize; its refusal leaves a prepared stage.
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        status = get_status(workspace, proposal_public_id=proposal_public_id)
        assert status["finalization_state"] == "prepared_pending_authorization"
        assert (
            status["calculation_snapshot_hash"]
            == refused.response["error"]["details"]["calculation_snapshot_hash"]
        )

        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        authorize = run_authorize(workspace, review, expected_hash)
        assert authorize.exit_code == bridge_errors.EXIT_OK
        status = get_status(workspace, proposal_public_id=proposal_public_id)
        assert status["finalization_state"] == "authorized_pending_finalization"
        assert status["authorization_id"] == authorize.response["result"]["authorization_id"]

        finalize = run_finalize(workspace, review)
        assert finalize.exit_code == bridge_errors.EXIT_OK
        status = get_status(workspace, proposal_public_id=proposal_public_id)
        assert status["finalization_state"] == "finalized"
        assert status["final_transaction_created"] is True
        assert (
            status["transaction_public_id"] == finalize.response["result"]["transaction_public_id"]
        )

    def test_unconfirmed_and_missing_readiness_states(self, workspace, monkeypatch) -> None:
        capture = setup_text_proposal(workspace, "lunch 12.50", "status-unconfirmed")
        status = get_status(workspace, proposal_public_id=capture["proposal_public_id"])
        assert status["finalization_state"] == "unconfirmed"

        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        status = get_status(workspace, proposal_public_id=review["proposal_public_id"])
        # Not converted yet: the receipt registry row does not exist, so the
        # state stays visibly incomplete without readiness detail.
        assert status["finalization_state"] == "confirmed_incomplete"
        assert status["final_transaction_created"] is False


# ---------------------------------------------------------------------------
# Review-finding coverage: contract matrix rows 4/6/7/15/19/24/25/27 extras
# ---------------------------------------------------------------------------


def mutate_receipt_payload_to_shared(
    workspace: support.BridgeWorkspace, proposal_public_id: str
) -> None:
    """Test-only synthetic drift: flip the durable payload classification."""
    conn = support.open_database(workspace)
    try:
        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
            (proposal_public_id,),
        ).fetchone()
        payload = json.loads(row["parsed_payload"])
        payload["transaction_type"] = "shared_expense"
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
            (json.dumps(payload), proposal_public_id),
        )
        conn.commit()
    finally:
        conn.close()


def mutate_receipt_payload_to_unknown(
    workspace: support.BridgeWorkspace, proposal_public_id: str
) -> None:
    """Test-only synthetic drift: make parser classification ambiguous."""
    conn = support.open_database(workspace)
    try:
        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
            (proposal_public_id,),
        ).fetchone()
        payload = json.loads(row["parsed_payload"])
        payload["transaction_type"] = "personal_expense"
        payload["participants"] = [SELF_PARTICIPANT]
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
            (json.dumps(payload), proposal_public_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestMatrixGapCoverage:
    def test_finalize_rejects_rejected_proposal(self, workspace) -> None:
        """Matrix row 4: rejected proposals stay not_confirmed forever."""
        capture = setup_text_proposal(workspace, "lunch 12.50", "row4-rejected")
        review = get_review(workspace, capture["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "reject")
        outcome = support.run_cli(
            support.make_request(
                "reject", arguments, idempotency_key=decision_key("reject", review)
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        fresh_review = get_review(workspace, capture["proposal_public_id"])
        finalized = run_finalize(workspace, fresh_review)
        assert finalized.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert finalized.response["error"]["details"]["reason"] == "not_confirmed"

    def test_shared_classification_receipt_is_refused(self, workspace, monkeypatch) -> None:
        """Matrix row 6: shared classification fails closed, zero writes."""
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        mutate_receipt_payload_to_shared(workspace, review["proposal_public_id"])
        drifted = get_status(workspace, proposal_public_id=review["proposal_public_id"])
        arguments = finalize_arguments(workspace, drifted)
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        error = outcome.response["error"]
        assert error["code"] == bridge_errors.FINALIZATION_REFUSED
        assert error["details"]["reason"] == "shared_receipt_refused"
        conn = support.open_database(workspace)
        try:
            conversions = int(
                conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0]
            )
            assert conversions == 0
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
        finally:
            conn.close()

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (mutate_receipt_payload_to_shared, "shared_receipt_refused"),
            (mutate_receipt_payload_to_unknown, "personal_only_violated"),
        ],
    )
    def test_every_s5d_receipt_writer_refuses_non_personal_classification_before_writes(
        self, workspace, monkeypatch, mutate, reason
    ) -> None:
        """D1b, snapshot review, and D2B share the personal-only gate."""
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        mutate(workspace, review["proposal_public_id"])
        drifted = get_status(workspace, proposal_public_id=review["proposal_public_id"])

        outcomes = (
            run_prepare_receipt_completion(workspace, drifted),
            run_apply_fact_set(workspace, drifted, "missing.json"),
            run_snapshot_review(workspace, drifted),
            run_authorize(workspace, drifted, "0" * 64),
        )

        for outcome in outcomes:
            assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
            assert outcome.response["error"]["code"] == bridge_errors.FINALIZATION_REFUSED
            assert outcome.response["error"]["details"]["reason"] == reason
        assert table_counts(
            workspace,
            (
                "receipt_proposal_conversions",
                "receipt_item_allocation_fact_sets",
                "authoritative_calculation_snapshots",
                "receipt_finalization_authorizations",
                "transactions",
            ),
        ) == {
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "transactions": 0,
        }

    def test_multiple_participants_refused(self, workspace, monkeypatch) -> None:
        """Matrix row 7 full: personal_only_violated with two active participants."""
        manifest = parse_runner_manifest(
            json.dumps(
                {
                    "schema_version": "v1",
                    "workspace_identity": "ws_bridge_test",
                    "operator_actor_id": OPERATOR,
                    "participants": [
                        {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
                        {"public_id": "ptcp_guest", "display_name": "Guest", "is_self": False},
                    ],
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        conn = support.open_database(workspace)
        try:
            bootstrap_participants(conn, manifest)
            conn.commit()
        finally:
            conn.close()
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        outcome = run_finalize(workspace, review)
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["details"]["reason"] == "personal_only_violated"

    def test_mid_finalization_failure_leaves_zero_partial_facts(
        self, workspace, monkeypatch
    ) -> None:
        """Matrix row 15: injected failure at the guarded boundary rolls back."""
        from finance_core.openclaw_staging_bridge import commands as bridge_commands

        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        authorize = run_authorize(workspace, review, expected_hash)
        assert authorize.exit_code == bridge_errors.EXIT_OK

        def exploding_finalizer(_conn, _authorization):
            raise RuntimeError("injected mid-finalization failure")

        original_finalizer = bridge_commands.finalize_prepared_receipt
        monkeypatch.setattr(bridge_commands, "finalize_prepared_receipt", exploding_finalizer)
        failed = run_finalize(workspace, review)
        assert failed.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert failed.response["error"]["code"] == bridge_errors.FINALIZATION_REFUSED
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
            audit = conn.execute(
                "SELECT COUNT(*) FROM receipt_finalization_audit WHERE status = 'finalized'"
            ).fetchone()
            assert int(audit[0]) == 0
        finally:
            conn.close()
        # Recovery: the unpatched retry finalizes exactly once.
        monkeypatch.setattr(bridge_commands, "finalize_prepared_receipt", original_finalizer)
        recovered = run_finalize(workspace, review)
        assert recovered.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 1
        finally:
            conn.close()

    def test_fact_set_supersession_after_prepare_requires_re_review(
        self, workspace, monkeypatch
    ) -> None:
        """Matrix row 24: supersession invalidates the reviewed hash."""
        from finance_core.parser_proposals.receipt_item_allocation_facts import (
            ReceiptItemAllocationFactsSupersessionCommand,
            supersede_receipt_item_allocation_facts,
        )

        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        apply = apply_human_fact_set(workspace, review)
        assert apply.exit_code == bridge_errors.EXIT_OK
        fact_set = apply.response["result"]
        refused = run_finalize(workspace, review)
        assert refused.response["error"]["code"] == bridge_errors.SNAPSHOT_AUTHORIZATION_REQUIRED
        old_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]

        # Human author persists a corrected replacement fact set (v2).
        command_id, receipt_id = receipt_identities(review)
        conn = support.open_database(workspace)
        try:
            registry = conn.execute(
                "SELECT conversion_result_hash FROM receipt_proposal_conversions "
                "WHERE command_public_id = ?",
                (command_id,),
            ).fetchone()
            conversion_result_hash = str(registry["conversion_result_hash"])
            supersession = ReceiptItemAllocationFactsSupersessionCommand.from_mapping(
                {
                    "command_public_id": "riafc_bridge_test_sup_0001",
                    "receipt_public_id": receipt_id,
                    "expected_conversion_command_public_id": command_id,
                    "expected_conversion_result_hash": conversion_result_hash,
                    "expected_current_fact_set_public_id": fact_set["fact_set_public_id"],
                    "expected_current_fact_set_result_hash": fact_set["fact_set_result_hash"],
                    "items": [
                        {
                            "line_number": 1,
                            "item_name": "Total corrected",
                            "line_amount": "12.34",
                            "currency": "SGD",
                        }
                    ],
                    "allocations": [
                        {
                            "line_number": 1,
                            "allocation_method": "manual",
                            "participants": [
                                {
                                    "participant_public_id": SELF_PARTICIPANT,
                                    "share_amount": "12.34",
                                    "currency": "SGD",
                                }
                            ],
                        }
                    ],
                    "adjustments": [],
                    "authenticated_actor_id": OPERATOR,
                    "actor_type": "human",
                    "channel": BRIDGE_CHANNEL,
                    "schema_version": "v1",
                }
            )
            supersede_receipt_item_allocation_facts(conn, supersession)
            conn.commit()
        finally:
            conn.close()

        # The reviewed hash no longer binds: zero-write STALE_SNAPSHOT.
        stale = run_authorize(workspace, review, old_hash)
        assert stale.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert stale.response["error"]["code"] == bridge_errors.STALE_SNAPSHOT

        # Recovery: finalize re-presents the new snapshot; re-authorize; done.
        re_refused = run_finalize(workspace, review)
        assert re_refused.response["error"]["code"] == bridge_errors.SNAPSHOT_AUTHORIZATION_REQUIRED
        new_hash = re_refused.response["error"]["details"]["calculation_snapshot_hash"]
        assert new_hash != old_hash
        finalized = authorize_and_finalize(workspace, review, new_hash)
        assert finalized.exit_code == bridge_errors.EXIT_OK
        assert finalized.response["result"]["calculation_snapshot_hash"] == new_hash

    def test_authorization_material_conflict_is_refused(self, workspace, monkeypatch) -> None:
        """Matrix row 25: drifted durable authorization material conflicts."""
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        authorize = run_authorize(workspace, review, expected_hash)
        assert authorize.exit_code == bridge_errors.EXIT_OK
        authorization_id = authorize.response["result"]["authorization_id"]

        conn = support.open_database(workspace)
        try:
            conn.execute(
                "UPDATE receipt_finalization_authorizations SET content_hash = ? "
                "WHERE authorization_id = ?",
                ("ab" * 32, authorization_id),
            )
            conn.commit()
        finally:
            conn.close()
        conflict = run_authorize(workspace, review, expected_hash)
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["details"]["reason"] == "authorization_conflict"

    def test_authorize_replay_is_idempotent(self, workspace, monkeypatch) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        first = run_authorize(workspace, review, expected_hash)
        assert first.exit_code == bridge_errors.EXIT_OK
        second = run_authorize(workspace, review, expected_hash)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert (
            second.response["result"]["authorization_id"]
            == first.response["result"]["authorization_id"]
        )
        conn = support.open_database(workspace)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM receipt_finalization_authorizations"
            ).fetchone()
            assert int(rows[0]) == 1
        finally:
            conn.close()

    @pytest.mark.parametrize("extra_field", ["amount", "items", "participants", "snapshot"])
    def test_finalize_rejects_monetary_argument_fields(self, workspace, extra_field) -> None:
        """Matrix row 27: every monetary/IAF argument field is refused."""
        review = setup_convertible_text_proposal(workspace)
        arguments = finalize_arguments(workspace, review)
        arguments[extra_field] = "material"
        outcome = support.run_cli(
            support.make_request(
                "finalize",
                arguments,
                idempotency_key=f"bridge-finalize:{review['proposal_public_id']}",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    @pytest.mark.parametrize("command", ["finalize", "authorize_finalization", "apply_fact_set"])
    def test_s4_commands_refuse_repository_internal_workspace(
        self, workspace, monkeypatch, command
    ) -> None:
        """Matrix row 19: workspace refusal before any I/O for every S4 command."""
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        proposal_public_id = review["proposal_public_id"]
        repo_root = str(Path(__file__).resolve().parents[1])
        if command == "finalize":
            arguments = finalize_arguments(workspace, review)
            key = f"bridge-finalize:{proposal_public_id}"
        elif command == "authorize_finalization":
            arguments = {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
                "operator_actor_id": OPERATOR,
                "expected_calculation_snapshot_hash": "ab" * 32,
            }
            key = f"bridge-authorize-finalization:{proposal_public_id}"
        else:
            arguments = {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
                "operator_actor_id": OPERATOR,
                "command_filename": "fact-set.json",
            }
            key = f"bridge-apply-fact-set:{proposal_public_id}"
        arguments["workspace_path"] = repo_root
        outcome = support.run_cli(support.make_request(command, arguments, idempotency_key=key))
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] in (
            bridge_errors.WORKSPACE_REFUSED,
            bridge_errors.WORKSPACE_MISSING,
        )

    def test_replay_flags_are_surfaced(self, workspace) -> None:
        """Matrix row 2: idempotent replays are observable in the envelope."""
        review = setup_convertible_text_proposal(workspace)
        first = run_finalize(workspace, review)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert first.response.get("idempotent_replay") is False
        second = run_finalize(workspace, review)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response.get("idempotent_replay") is True
