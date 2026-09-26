"""Acceptance regression tests for the eight PR #263 S1-S3 review findings.

Each class binds one acceptance finding to deterministic, reproducible
regression coverage: the authoritative expected-state guard closing the
confirmation TOCTOU window, the envelope-free OCR engine boundary, canonical
idempotency binding for every mutation, the structural private-DM Telegram
boundary, parser-authoritative personal/shared classification, authenticated
decision replay, the owning-module repository query boundary, and strictly
read-only review/status/health commands.  All data is temporary and synthetic;
nothing touches live data, credentials, the network, or OpenClaw deployment.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.attachment_evidence import get_telegram_source_evidence_for_raw_intake
from finance_core.intake.raw_text_repository import get_raw_intake_record_by_public_id
from finance_core.intake.receipt_ocr_proposal import get_receipt_ocr_extraction_status_for_proposal
from finance_core.openclaw_staging_bridge import commands as bridge_commands
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import identity as bridge_identity
from finance_core.openclaw_staging_bridge import ocr_boundary
from finance_core.parser_proposals.completion import complete_proposal, get_completion_by_public_id
from finance_core.parser_proposals.receipt_supersession import (
    get_receipt_proposal_revision_by_correction_id,
)
from finance_core.parser_proposals.repository import (
    ParserAuthorizationRepository,
    ParserProposalRepository,
)
from finance_core.parser_proposals.service import (
    StaleProposalDecisionStateError,
    confirm_parser_proposal,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def key_path(workspace: support.BridgeWorkspace) -> Path:
    return workspace.workspace_path / "runtime" / "callback_signing.key"


def setup_text_proposal(workspace: support.BridgeWorkspace, text: str) -> dict:
    update = support.telegram_text_update(text)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.authenticated_text_capture_arguments(workspace, update),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK, capture.response
    processed = support.process_captured_text(workspace, capture)
    assert processed.exit_code == bridge_errors.EXIT_OK, processed.response
    capture_result = capture.response["result"]
    capture_job = processed.response["result"]["capture_job"]
    assert capture_job["proposal_public_id"] is not None
    return {
        **capture_result,
        "capture_job": capture_job,
        "proposal_public_id": capture_job["proposal_public_id"],
    }


def get_review(workspace: support.BridgeWorkspace, proposal_public_id: str) -> dict:
    outcome = support.run_cli(
        support.make_request(
            "get_review",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
            },
        )
    )
    assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
    return outcome.response["result"]


def decision_arguments(
    workspace: support.BridgeWorkspace,
    review: dict,
    action: str,
    *,
    actor: str = "person_owner",
    token_override: str | None = None,
    version_override: int | None = None,
    hash_override: str | None = None,
    expiry_override: int | None = None,
) -> dict:
    entry = review["callback_tokens"][action]
    return {
        "workspace_path": str(workspace.workspace_path),
        "proposal_public_id": review["proposal_public_id"],
        "operator_actor_id": actor,
        "proposal_version": (
            version_override if version_override is not None else review["proposal_version"]
        ),
        "content_hash": (
            hash_override if hash_override is not None else review["effective_content_hash"]
        ),
        "callback_token": token_override if token_override is not None else entry["token"],
        "callback_expiry": (expiry_override if expiry_override is not None else entry["expiry"]),
    }


def decision_key(action: str, review: dict) -> str:
    if action == "edit":
        return support.canonical_edit_key(
            proposal_public_id=review["proposal_public_id"],
            version=review["proposal_version"],
            content_hash=review["effective_content_hash"],
        )
    return support.canonical_decision_key(
        action=action, proposal_public_id=review["proposal_public_id"]
    )


def decision_write_counts(workspace: support.BridgeWorkspace) -> dict[str, int]:
    conn = support.open_database(workspace)
    try:
        return {
            "authorizations": conn.execute(
                "SELECT COUNT(*) FROM parser_proposal_authorizations"
            ).fetchone()[0],
            "confirmations": conn.execute(
                "SELECT COUNT(*) FROM parser_proposal_confirmations"
            ).fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM parser_proposal_events").fetchone()[0],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finding 1: authoritative expected-state guard closes the confirmation TOCTOU
# ---------------------------------------------------------------------------


class TestExpectedStateGuardClosesToctou:
    def test_stale_callback_cannot_confirm_content_edited_inside_window(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = setup_text_proposal(workspace, "groceries 42")
        review = get_review(workspace, capture["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "confirm")

        real_verify = bridge_commands._verify_callback_context
        fired: list[dict[str, int]] = []

        def racing_verify(
            conn: Any,
            workspace_path: Any,
            validated: dict[str, Any],
            *,
            action: str,
            deadline: Any,
            terminal_guard: bool = True,
        ) -> dict[str, Any]:
            proposal = real_verify(
                conn,
                workspace_path,
                validated,
                action=action,
                deadline=deadline,
                terminal_guard=terminal_guard,
            )
            if not fired:
                # Interleave a legitimate edit between token verification and
                # the decision transaction: the old callback token now points
                # at content the operator never reviewed.
                complete_proposal(
                    conn,
                    int(proposal["id"]),
                    actor="person_owner",
                    expected_content_hash=validated["content_hash"],
                    field_updates={"merchant": "Interleaved Merchant"},
                    completion_public_id=bridge_identity.completion_public_id("race-edit"),
                    completion_channel="openclaw_staging_bridge",
                )
                fired.append(decision_write_counts(workspace))
            return proposal

        monkeypatch.setattr(bridge_commands, "_verify_callback_context", racing_verify)
        outcome = support.run_cli(
            support.make_request(
                "confirm", arguments, idempotency_key=decision_key("confirm", review)
            )
        )
        assert fired, "the interleaving shim never ran; the test is not exercising the window"
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_CONTENT_HASH
        assert outcome.response["error"]["retryable"] is False

        # The refused decision wrote nothing beyond the interleaved edit's own
        # lifecycle event: zero authorization, zero confirmation, zero decision
        # event, and the proposal stays pending confirmation.
        after = decision_write_counts(workspace)
        assert after == fired[0]
        assert after["authorizations"] == 0
        assert after["confirmations"] == 0
        conn = support.open_database(workspace)
        try:
            parse_status = conn.execute("SELECT parse_status FROM parser_outputs").fetchone()[
                "parse_status"
            ]
            assert parse_status == "edited_pending_confirmation"
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

    def test_service_guard_refuses_mismatched_expected_state_with_zero_writes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "metro 3.20")
        conn = support.open_database(workspace)
        try:
            proposal = ParserProposalRepository(conn).get_by_public_id(
                capture["proposal_public_id"]
            )
            assert proposal is not None
            proposal_id = int(proposal["id"])
            status_before = str(proposal["parse_status"])
            with pytest.raises(StaleProposalDecisionStateError):
                confirm_parser_proposal(
                    conn,
                    proposal_id,
                    authenticated_actor_id="person_owner",
                    confirmation_channel="test",
                    confirmation_public_id="pca_bridge_test_guard",
                    expected_content_hash="0" * 64,
                    expected_version=99,
                )
            assert ParserAuthorizationRepository(conn).get_for_proposal(proposal_id) is None
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_proposal_confirmations").fetchone()[0]
                == 0
            )
            assert conn.execute("SELECT COUNT(*) FROM parser_proposal_events").fetchone()[0] == 0
            assert (
                str(
                    conn.execute(
                        "SELECT parse_status FROM parser_outputs WHERE id = ?", (proposal_id,)
                    ).fetchone()["parse_status"]
                )
                == status_before
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Finding 4: structural private-DM Telegram boundary
# ---------------------------------------------------------------------------


class TestTelegramPrivateDmBoundary:
    def test_private_text_dm_succeeds(self, workspace: support.BridgeWorkspace) -> None:
        update = support.telegram_text_update("coffee 4.50")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.authenticated_text_capture_arguments(workspace, update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response

    def test_openclaw_normalized_private_text_omits_unavailable_update_id(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("coffee 4.50")
        arguments = support.authenticated_text_capture_arguments(workspace, update)
        arguments["telegram_message"] = arguments.pop("telegram_update")["message"]
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        conn = support.open_database(workspace)
        try:
            row = conn.execute("SELECT source_payload FROM raw_intake_evidence").fetchone()
            assert row is not None
            assert "telegram_update_id" not in row["source_payload"]
        finally:
            conn.close()

    def test_text_capture_requires_exactly_one_transport_shape(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("coffee 4.50")
        for arguments in (
            {"workspace_path": str(workspace.workspace_path), "kind": "text"},
            {
                "workspace_path": str(workspace.workspace_path),
                "kind": "text",
                "telegram_update": update,
                "telegram_message": update["message"],
            },
        ):
            outcome = support.run_cli(
                support.make_request(
                    "capture",
                    arguments,
                    idempotency_key=support.canonical_capture_key(message_id=10),
                )
            )
            assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
            assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    @pytest.mark.parametrize(
        ("chat_id", "chat_type", "sender_id"),
        [
            (-100_123_456, "supergroup", -100_123_456),
            (-555, "group", -555),
            (111, "group", 111),
            (111, "channel", 111),
            (111, None, 222),
        ],
    )
    def test_text_group_and_mismatch_shapes_are_refused_without_persistence(
        self,
        workspace: support.BridgeWorkspace,
        chat_id: int,
        chat_type: str | None,
        sender_id: int,
    ) -> None:
        update = support.telegram_text_update(
            "group message", chat_id=chat_id, chat_type=chat_type, sender_id=sender_id
        )
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(workspace, update),
                idempotency_key=support.canonical_capture_key(
                    chat_id=chat_id if chat_id > 0 else 111, message_id=10
                ),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.TELEGRAM_SOURCE_REFUSED
        assert outcome.response["error"]["retryable"] is False

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        finally:
            conn.close()

    def test_private_receipt_dm_succeeds(self, workspace: support.BridgeWorkspace) -> None:
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response

    @pytest.mark.parametrize(
        ("chat_id", "sender_id"),
        [
            (0, 111),
            (111, 222),
        ],
    )
    def test_receipt_group_and_mismatch_shapes_are_refused_without_persistence(
        self,
        workspace: support.BridgeWorkspace,
        chat_id: int,
        sender_id: int,
    ) -> None:
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace, handoff_filename="receipt.jpg", chat_id=chat_id, sender_id=sender_id
                ),
                idempotency_key=support.canonical_capture_key(
                    chat_id=chat_id if chat_id > 0 else 111, message_id=20
                ),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.TELEGRAM_SOURCE_REFUSED

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        finally:
            conn.close()

    def test_receipt_capture_without_sender_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace, handoff_filename="receipt.jpg", sender_id=None
                ),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.TELEGRAM_SOURCE_REFUSED


# ---------------------------------------------------------------------------
# Finding 5: personal/shared classification uses parser-authoritative semantics
# ---------------------------------------------------------------------------


class TestClassificationUsesParserSemantics:
    def test_personal_with_paid_by_is_personal(self) -> None:
        # Counterexample from acceptance: "Lunch 12 SGD paid by Owner" parses as
        # personal with a payer present; payer presence must not flip it.
        classification, unknown = bridge_commands._classification(
            {"transaction_type": "personal_expense", "paid_by": "Owner"}
        )
        assert classification == "personal"
        assert unknown is False

    def test_shared_without_paid_by_is_shared(self) -> None:
        # Counterexample from acceptance: "Dinner 30 SGD shared equally with
        # Alice" parses as shared with no payer; absence of paid_by must not
        # flip it.
        classification, unknown = bridge_commands._classification(
            {
                "transaction_type": "shared_expense",
                "participants": ["Alice"],
                "split_type": "equal",
            }
        )
        assert classification == "shared"
        assert unknown is False

    def test_conflicting_signals_are_unknown(self) -> None:
        classification, unknown = bridge_commands._classification(
            {
                "transaction_type": "personal_expense",
                "participants": ["Alice"],
                "split_type": "equal",
            }
        )
        assert classification == "unknown"
        assert unknown is True

    def test_missing_signals_are_unknown(self) -> None:
        classification, unknown = bridge_commands._classification({})
        assert classification == "unknown"
        assert unknown is True

    def test_review_lunch_paid_by_owner_is_personal(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "Lunch 12 SGD paid by Owner")
        review = get_review(workspace, capture["proposal_public_id"])
        assert review["classification"] == "personal"
        assert "unknown_classification" not in review["ambiguity_indicators"]

    def test_review_dinner_shared_with_alice_is_shared(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "Dinner 30 SGD shared equally with Alice")
        review = get_review(workspace, capture["proposal_public_id"])
        assert review["classification"] == "shared"
        assert "unknown_classification" not in review["ambiguity_indicators"]


# ---------------------------------------------------------------------------
# Finding 3: canonical idempotency binding for every mutation
# ---------------------------------------------------------------------------


class TestCanonicalIdempotencyBinding:
    def test_capture_conflict_creates_no_additional_rows(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        first_update = support.telegram_text_update("original text")
        first = support.run_cli(
            support.make_request(
                "capture",
                support.authenticated_text_capture_arguments(workspace, first_update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert first.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            intake_before = conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
            evidence_before = conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0]
            proposal_before = conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0]
        finally:
            conn.close()

        conflict_update = support.telegram_text_update("different text")
        conflict = support.run_cli(
            support.make_request(
                "capture",
                support.authenticated_text_capture_arguments(workspace, conflict_update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert conflict.response["error"]["retryable"] is False

        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
                == intake_before
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0]
                == evidence_before
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == proposal_before
            )
        finally:
            conn.close()

    def test_propose_same_key_same_arguments_replays(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "bus 2.10")
        arguments = {
            "workspace_path": str(workspace.workspace_path),
            "intake_public_id": capture["intake_public_id"],
        }
        request = support.make_request(
            "propose",
            arguments,
            idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert second.exit_code == bridge_errors.EXIT_OK
        assert (
            second.response["result"]["proposal_public_id"]
            == first.response["result"]["proposal_public_id"]
        )

    def test_propose_non_canonical_key_conflicts_without_writes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "tram 3.40")
        conn = support.open_database(workspace)
        try:
            proposal_before = conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0]
        finally:
            conn.close()
        conflict = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key="non-canonical-propose-key",
            )
        )
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == proposal_before
            )
        finally:
            conn.close()

    def test_confirm_key_reuse_across_proposals_is_refused_without_writes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture_a = setup_text_proposal(workspace, "coffee 6")
        review_a = get_review(workspace, capture_a["proposal_public_id"])
        confirm_a = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review_a, "confirm"),
                idempotency_key=decision_key("confirm", review_a),
            )
        )
        assert confirm_a.exit_code == bridge_errors.EXIT_OK

        # Second proposal in a fresh message identity.
        second_update = support.telegram_text_update("tea 5", message_id=11)
        capture_b = support.run_cli(
            support.make_request(
                "capture",
                support.authenticated_text_capture_arguments(workspace, second_update),
                idempotency_key=support.canonical_capture_key(message_id=11),
            )
        )
        assert capture_b.exit_code == bridge_errors.EXIT_OK
        processed_b = support.process_captured_text(workspace, capture_b)
        assert processed_b.exit_code == bridge_errors.EXIT_OK, processed_b.response
        proposal_b = processed_b.response["result"]["capture_job"]["proposal_public_id"]
        assert proposal_b is not None
        review_b = get_review(workspace, proposal_b)
        before = decision_write_counts(workspace)

        # Reusing proposal A's canonical confirm key against proposal B must
        # refuse before any lookup: previously this could collide on the
        # derived confirmation ID and surface as INTERNAL_ERROR.
        conflict = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review_b, "confirm"),
                idempotency_key=decision_key("confirm", review_a),
            )
        )
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert decision_write_counts(workspace) == before

        conn = support.open_database(workspace)
        try:
            statuses = {
                row["parse_status"]
                for row in conn.execute("SELECT parse_status FROM parser_outputs").fetchall()
            }
            assert "parsed_pending_confirmation" in statuses
        finally:
            conn.close()

    def test_edit_same_key_different_fields_conflicts_without_writes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "hardware 45")
        review = get_review(workspace, capture["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"merchant": "Sunrise Market"}
        first = support.run_cli(
            support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
        )
        assert first.exit_code == bridge_errors.EXIT_OK
        before = decision_write_counts(workspace)

        conflicting = decision_arguments(workspace, review, "edit")
        conflicting["field_updates"] = {"merchant": "Different Merchant"}
        outcome = support.run_cli(
            support.make_request("edit", conflicting, idempotency_key=decision_key("edit", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert decision_write_counts(workspace) == before

    def test_reject_same_key_different_actor_conflicts_without_writes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "spam message")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "reject",
            decision_arguments(workspace, review, "reject"),
            idempotency_key=decision_key("reject", review),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert first.response["idempotent_replay"] is False
        replay = support.run_cli(request)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True
        before = decision_write_counts(workspace)

        conflict = support.run_cli(
            support.make_request(
                "reject",
                decision_arguments(workspace, review, "reject", actor="someone_else"),
                idempotency_key=decision_key("reject", review),
            )
        )
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert decision_write_counts(workspace) == before


# ---------------------------------------------------------------------------
# Finding 6: decision replay never bypasses callback authentication
# ---------------------------------------------------------------------------


class TestDecisionReplayRequiresAuthentication:
    def test_identical_replay_is_the_legal_double_click(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "snack 3")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        second = support.run_cli(request)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True
        assert (
            second.response["result"]["confirmation_id"]
            == first.response["result"]["confirmation_id"]
        )

    def test_replay_with_expired_token_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "juice 4")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", expiry_override=1_700_000_000),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED

    def test_replay_with_forged_token_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "water 2")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        token = review["callback_tokens"]["confirm"]["token"]
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", token_override=forged),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.CALLBACK_TOKEN_INVALID

    def test_replay_with_stale_content_hash_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "bagel 5")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", hash_override="0" * 64),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.STALE_CONTENT_HASH

    def test_replay_with_stale_version_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "noodles 9")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", version_override=9),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.STALE_VERSION

    def test_replay_without_idempotency_key_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "rice 7")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        replay = support.run_cli(
            support.make_request("confirm", decision_arguments(workspace, review, "confirm"))
        )
        assert replay.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert replay.response["error"]["code"] == bridge_errors.MISSING_IDEMPOTENCY_KEY

    def test_replay_with_different_idempotency_key_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "curry 11")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key="another-confirm-key",
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_replay_with_different_actor_under_same_key_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "taco 8")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        assert support.run_cli(request).exit_code == bridge_errors.EXIT_OK
        before = decision_write_counts(workspace)
        replay = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", actor="someone_else"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert replay.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert decision_write_counts(workspace) == before


# ---------------------------------------------------------------------------
# Finding 7: bridge reads other modules only through owning repositories
# ---------------------------------------------------------------------------


class TestRepositoryQueryBoundary:
    def test_commands_module_contains_no_direct_sql(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        source = (Path(bridge_commands.__file__)).read_text(encoding="utf-8")
        assert "conn.execute" not in source
        assert ".execute(" not in source
        assert "SELECT " not in source
        assert "INSERT INTO" not in source

    def test_repository_apis_supply_review_status_and_replay_state(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        monkeypatch.setattr(
            ocr_boundary, "engine_factory", lambda _workspace: support.FakeOcrEngine()
        )
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
        assert propose.exit_code == bridge_errors.EXIT_OK, propose.response
        proposal_public_id = propose.response["result"]["proposal_public_id"]

        # One completion edit gives the replay lookups a durable row to resolve.
        review = get_review(workspace, proposal_public_id)
        edit_arguments = decision_arguments(workspace, review, "edit")
        edit_arguments["field_updates"] = {"merchant": "Boundary Market"}
        edit = support.run_cli(
            support.make_request(
                "edit", edit_arguments, idempotency_key=decision_key("edit", review)
            )
        )
        assert edit.exit_code == bridge_errors.EXIT_OK, edit.response

        conn = support.open_database(workspace)
        try:
            proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
            assert proposal is not None
            assert proposal["source_public_id"] == intake_public_id
            assert str(proposal["parse_status"]) == "edited_pending_confirmation"

            intake = get_raw_intake_record_by_public_id(conn, intake_public_id)
            assert intake is not None
            assert intake["parser_output_id"] == proposal["id"]
            assert intake["source_channel"] == "telegram"

            evidence = get_telegram_source_evidence_for_raw_intake(conn, int(intake["id"]))
            assert evidence is not None
            assert evidence["content_hash"] == support.sha256_hex(support.JPEG_BYTES)

            status = get_receipt_ocr_extraction_status_for_proposal(conn, int(proposal["id"]))
            assert status == "succeeded"

            # Edit replay reconstruction resolves through the owning modules.
            completion = get_completion_by_public_id(
                conn, bridge_identity.completion_public_id(decision_key("edit", review))
            )
            assert completion is not None
            assert completion["base_content_hash"] == review["effective_content_hash"]
            assert get_completion_by_public_id(conn, "pcf_bridge_missing") is None
            assert (
                get_receipt_proposal_revision_by_correction_id(conn, "prv_bridge_missing") is None
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Finding 2: OCR engine never comes from the request envelope
# ---------------------------------------------------------------------------


class TestOcrEnvelopeBoundary:
    def test_propose_envelope_cannot_supply_ocr_helper(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "receiptless 1")
        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                    "ocr_helper_path": "/tmp/attacker/helper",
                    "ocr_helper_expected_version": "9.9.9",
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_production_resolver_fails_closed_without_trusted_config(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The production path resolves through the trusted workspace runtime
        # boundary (never monkeypatched): without a wired configuration it
        # fails closed.
        monkeypatch.setattr(
            ocr_boundary, "engine_factory", ocr_boundary.resolve_workspace_ocr_engine
        )
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        capture = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert capture.exit_code == bridge_errors.EXIT_OK
        propose = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture.response["result"]["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(
                    capture.response["result"]["intake_public_id"]
                ),
            )
        )
        assert propose.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert propose.response["error"]["code"] == bridge_errors.OCR_ENGINE_UNAVAILABLE
        assert propose.response["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# Finding 8: read commands stay strictly read-only
# ---------------------------------------------------------------------------


class TestReadCommandsStayReadOnly:
    def test_get_review_fails_closed_without_key_and_never_creates_one(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "readonly 5")
        assert key_path(workspace).exists()
        key_path(workspace).unlink()
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": capture["proposal_public_id"],
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_KEY_MISSING
        assert not key_path(workspace).exists()

    def test_health_reports_missing_key_without_creating_one(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.run_cli(support.make_request("health", support.health_arguments(workspace)))
        assert key_path(workspace).exists()
        key_path(workspace).unlink()
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace))
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        assert outcome.response["result"]["callback_key_status"] == "missing"
        assert not key_path(workspace).exists()

    def test_health_reports_unsafe_key_without_repairing_it(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        callback_key = key_path(workspace)
        original_content_hash = support.sha256_hex(callback_key.read_bytes())
        callback_key.chmod(0o644)
        completed = subprocess.run(
            [sys.executable, "-m", "finance_core.openclaw_staging_bridge.cli"],
            input=json.dumps(support.make_request("health", support.health_arguments(workspace))),
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        assert completed.returncode == bridge_errors.EXIT_OK, completed.stderr
        response = json.loads(completed.stdout)
        assert response["result"]["callback_key_status"] == "unsafe"
        assert stat.S_IMODE(callback_key.stat().st_mode) == 0o644
        assert support.sha256_hex(callback_key.read_bytes()) == original_content_hash

    def test_get_status_reads_without_callback_key(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "status 6")
        key_path(workspace).unlink()
        outcome = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        assert outcome.response["result"]["intake_public_id"] == capture["intake_public_id"]
        assert not key_path(workspace).exists()
