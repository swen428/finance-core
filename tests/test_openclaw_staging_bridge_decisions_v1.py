"""S3 contract tests: decision commands and callback-token protection.

Covers confirm/edit/reject through the existing authoritative boundaries,
the stateless HMAC callback-token contract (expiry, tampering, wrong action,
stale version/hash, missing/unsafe key), terminal-state and conflict
semantics, lifecycle-reconstruction replay, workspace recovery, token
staleness after edits, concurrency, and the confirmation-versus-finalization
separation: no decision command may create a transaction, receipt fact,
calculation snapshot, authorization, or any final financial fact.  All data
is temporary and synthetic.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import ocr_boundary
from finance_core.receipt_staging_runner.models import RunnerWorkspaceError
from finance_core.receipt_staging_runner.workspace import (
    recover_runner_workspace,
    verify_callback_signing_key,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def key_path(workspace: support.BridgeWorkspace) -> Path:
    return workspace.workspace_path / "runtime" / "callback_signing.key"


def setup_text_proposal(workspace: support.BridgeWorkspace, text: str, key: str) -> dict:
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(workspace, support.telegram_text_update(text)),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    return capture.response["result"]


def setup_receipt_proposal(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> dict:
    support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    engine = support.FakeOcrEngine()
    monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
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
    assert propose.exit_code == bridge_errors.EXIT_OK
    return propose.response["result"]


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
    assert outcome.exit_code == bridge_errors.EXIT_OK
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
    """Canonical decision idempotency key binding the proposal and action.

    Edit keys additionally bind the pre-edit version and content hash so
    consecutive edit versions carry distinct idempotency identities.
    """
    if action == "edit":
        return support.canonical_edit_key(
            proposal_public_id=review["proposal_public_id"],
            version=review["proposal_version"],
            content_hash=review["effective_content_hash"],
        )
    return support.canonical_decision_key(
        action=action, proposal_public_id=review["proposal_public_id"]
    )


class TestConfirm:
    def test_deterministic_deny_intent_is_unavailable_for_review(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(
            workspace,
            "Lunch SGD 12.34 paid by Alice at Cafe",
            "deny-intent-review",
        )
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
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE

    def test_confirm_persists_decision_and_creates_no_final_fact(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "lunch 12.50", "confirm-1")
        review = get_review(workspace, capture["proposal_public_id"])

        conn = support.open_database(workspace)
        try:
            before = support.count_final_facts(conn)
        finally:
            conn.close()

        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        result = outcome.response["result"]
        assert result["decision"] == "confirmed"
        assert result["to_status"] == "confirmed"
        assert result["confirmation_id"].startswith("pca_bridge_")
        assert result["final_transaction_created"] is False
        rendered = json.dumps(outcome.response)
        support.assert_no_sensitive_material(rendered, workspace)

        conn = support.open_database(workspace)
        try:
            after = support.count_final_facts(conn)
            assert after == before
            assert all(count == 0 for count in after.values())
            authorization_count = conn.execute(
                "SELECT COUNT(*) FROM parser_proposal_authorizations"
            ).fetchone()[0]
            assert authorization_count == 1
            parse_status = conn.execute("SELECT parse_status FROM parser_outputs").fetchone()[
                "parse_status"
            ]
            assert parse_status == "confirmed"
        finally:
            conn.close()

    def test_identical_confirm_replays_original_result(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "coffee 6.00", "confirm-2")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True
        assert (
            second.response["result"]["confirmation_id"]
            == first.response["result"]["confirmation_id"]
        )
        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_contradictory_decision_after_confirm_fails_closed(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "dinner 30", "confirm-3")
        review = get_review(workspace, capture["proposal_public_id"])
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK

        reject = support.run_cli(
            support.make_request(
                "reject",
                decision_arguments(workspace, review, "reject"),
                idempotency_key=decision_key("reject", review),
            )
        )
        assert reject.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert reject.response["error"]["code"] == bridge_errors.LIFECYCLE_CONFLICT

    def test_concurrent_identical_confirms_create_one_authorization(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "taxi 15", "confirm-4")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )
        outcomes: list[support.CliOutcome] = []
        lock = threading.Lock()

        def worker() -> None:
            outcome = support.run_cli(request)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(outcome.exit_code == bridge_errors.EXIT_OK for outcome in outcomes)
        confirmation_ids = {outcome.response["result"]["confirmation_id"] for outcome in outcomes}
        assert len(confirmation_ids) == 1
        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0]
                == 1
            )
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()


class TestReject:
    def test_reject_is_terminal_and_creates_no_final_fact(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "not an expense", "reject-1")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "reject",
                decision_arguments(workspace, review, "reject"),
                idempotency_key=decision_key("reject", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["decision"] == "rejected"
        assert result["to_status"] == "rejected"
        assert result["final_transaction_created"] is False

        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
            parse_status = conn.execute("SELECT parse_status FROM parser_outputs").fetchone()[
                "parse_status"
            ]
            assert parse_status == "rejected"
        finally:
            conn.close()

    def test_reject_replay_returns_original_and_confirm_conflicts(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "spam", "reject-2")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "reject",
            decision_arguments(workspace, review, "reject"),
            idempotency_key=decision_key("reject", review),
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True

        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert confirm.response["error"]["code"] == bridge_errors.LIFECYCLE_CONFLICT


class TestCallbackTokenProtection:
    def test_expired_token_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "metro 4", "token-1")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", expiry_override=1_700_000_000),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED

    def test_tampered_token_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "bus 2", "token-2")
        review = get_review(workspace, capture["proposal_public_id"])
        token = review["callback_tokens"]["confirm"]["token"]
        flipped = (
            ("fcb_v1_AAAA" + token[len("fcb_v1_AAAA") :])
            if token.endswith("A")
            else (token[:-1] + ("A" if token[-1] != "A" else "B"))
        )
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", token_override=flipped),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_TOKEN_INVALID

    def test_malformed_token_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "tram 3", "token-3")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", token_override="not-a-token"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_TOKEN_INVALID

    def test_wrong_action_token_is_detected(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "ferry 8", "token-4")
        review = get_review(workspace, capture["proposal_public_id"])
        edit_token = review["callback_tokens"]["edit"]["token"]
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", token_override=edit_token),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_WRONG_ACTION

    def test_stale_version_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "train 20", "token-5")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", version_override=9),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_VERSION

    def test_stale_content_hash_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "cab 25", "token-6")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm", hash_override="0" * 64),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STALE_CONTENT_HASH

    def test_missing_key_refuses_decisions(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "ride 11", "token-7")
        review = get_review(workspace, capture["proposal_public_id"])
        key_path(workspace).unlink()
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_KEY_MISSING

    def test_unsafe_key_permissions_refuse_decisions(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "bike 1.50", "token-8")
        review = get_review(workspace, capture["proposal_public_id"])
        os.chmod(key_path(workspace), 0o644)
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_KEY_UNSAFE
        os.chmod(key_path(workspace), 0o600)

    def test_terminal_proposal_refuses_new_decisions(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "walk 0", "token-9")
        review = get_review(workspace, capture["proposal_public_id"])
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK

        # A fresh review of a terminal proposal carries no tokens.
        terminal_review = get_review(workspace, capture["proposal_public_id"])
        assert terminal_review["callback_tokens"] is None

        # A forged but structurally valid token cannot revive the proposal:
        # replay reconstruction returns the persisted decision only for the
        # identical canonical material; a different actor under the same
        # canonical key is a deterministic idempotency conflict.
        forged = decision_arguments(workspace, review, "confirm", actor="someone_else")
        outcome = support.run_cli(
            support.make_request("confirm", forged, idempotency_key=decision_key("confirm", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_key_material_never_appears_in_envelopes_or_diagnostics(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "tea 7", "token-10")
        review_outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": capture["proposal_public_id"],
                },
            )
        )
        review = review_outcome.response["result"]
        confirm_outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        key_bytes = key_path(workspace).read_bytes()
        key_hex = key_bytes.hex()
        for outcome in (review_outcome, confirm_outcome):
            assert key_hex not in json.dumps(outcome.response)
            assert key_hex not in outcome.stderr
            assert key_bytes.decode("latin-1") not in outcome.stderr


class TestEdit:
    def test_text_completion_edit_bumps_version_and_stales_tokens(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "groceries 88", "edit-1")
        review = get_review(workspace, capture["proposal_public_id"])

        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"merchant": "Sunrise Market", "category": "food"}
        outcome = support.run_cli(
            support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        result = outcome.response["result"]
        assert result["edit_kind"] == "completion"
        assert result["proposal_version"] == 1
        assert result["effective_content_hash"] != review["effective_content_hash"]
        assert result["parse_status"] == "edited_pending_confirmation"
        assert result["final_transaction_created"] is False

        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

        # Prior callback tokens are stale: the old version no longer verifies.
        stale_confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert stale_confirm.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert stale_confirm.response["error"]["code"] == bridge_errors.STALE_VERSION

        # A fresh review issues tokens bound to the new version/hash.
        new_review = get_review(workspace, capture["proposal_public_id"])
        assert new_review["proposal_version"] == 1
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, new_review, "confirm"),
                idempotency_key=decision_key("confirm", new_review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK

    def test_identical_edit_replays_and_conflicting_edit_fails(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "hardware 45", "edit-2")
        review = get_review(workspace, capture["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"merchant": "Tool Shop"}
        request = support.make_request(
            "edit", arguments, idempotency_key=decision_key("edit", review)
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True
        assert (
            second.response["result"]["proposal_version"]
            == first.response["result"]["proposal_version"]
        )

        # Same idempotency key with different updates fails closed.
        conflicting_arguments = decision_arguments(workspace, review, "edit")
        conflicting_arguments["field_updates"] = {"merchant": "Other Shop"}
        conflicting = support.run_cli(
            support.make_request(
                "edit", conflicting_arguments, idempotency_key=decision_key("edit", review)
            )
        )
        assert conflicting.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflicting.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_text_monetary_edit_is_refused_without_authoritative_boundary(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "gift 100", "edit-3")
        review = get_review(workspace, capture["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"amount": "200.00"}
        outcome = support.run_cli(
            support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.UNSUPPORTED_EDIT

    def test_account_and_intent_edits_are_refused(self, workspace: support.BridgeWorkspace) -> None:
        capture = setup_text_proposal(workspace, "movie 40", "edit-4")
        review = get_review(workspace, capture["proposal_public_id"])
        for forbidden in ({"account": "wallet"}, {"payer": "someone"}, {"intent": "x"}):
            arguments = decision_arguments(workspace, review, "edit")
            arguments["field_updates"] = forbidden
            outcome = support.run_cli(
                support.make_request(
                    "edit", arguments, idempotency_key=decision_key("edit", review)
                )
            )
            assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
            assert outcome.response["error"]["code"] == bridge_errors.UNSUPPORTED_EDIT

    def test_receipt_monetary_correction_supersedes_proposal(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        propose = setup_receipt_proposal(workspace, monkeypatch, "edit-5")
        review = get_review(workspace, propose["proposal_public_id"])

        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"amount": "56.78", "currency": "SGD"}
        outcome = support.run_cli(
            support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        result = outcome.response["result"]
        assert result["edit_kind"] == "receipt_monetary_correction"
        assert result["superseded_proposal_public_id"] == propose["proposal_public_id"]
        # The replacement identity is derived by the supersession boundary.
        assert result["proposal_public_id"].startswith("po_rev_")
        assert result["proposal_public_id"] != propose["proposal_public_id"]
        assert result["proposal_version"] == 0
        assert len(result["effective_content_hash"]) == 64
        assert result["final_transaction_created"] is False

        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
            parent_status = conn.execute(
                "SELECT parse_status FROM parser_outputs WHERE public_id = ?",
                (propose["proposal_public_id"],),
            ).fetchone()["parse_status"]
            assert parent_status == "superseded"
        finally:
            conn.close()

        # The replacement proposal can be confirmed with fresh tokens only.
        replacement_review = get_review(workspace, result["proposal_public_id"])
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, replacement_review, "confirm"),
                idempotency_key=decision_key("confirm", replacement_review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

    def test_edit_without_material_change_is_refused(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        propose = setup_receipt_proposal(workspace, monkeypatch, "edit-6")
        review = get_review(workspace, propose["proposal_public_id"])
        arguments = decision_arguments(workspace, review, "edit")
        arguments["field_updates"] = {"amount": "19.99", "currency": "SGD"}
        first = support.run_cli(
            support.make_request("edit", arguments, idempotency_key=decision_key("edit", review))
        )
        assert first.exit_code == bridge_errors.EXIT_OK

        # Echo the replacement's canonical amount/currency: no material change.
        replacement_review = get_review(workspace, first.response["result"]["proposal_public_id"])
        echo_arguments = decision_arguments(workspace, replacement_review, "edit")
        echo_arguments["field_updates"] = {"amount": "19.99", "currency": "SGD"}
        outcome = support.run_cli(
            support.make_request(
                "edit", echo_arguments, idempotency_key=decision_key("edit", replacement_review)
            )
        )
        assert outcome.exit_code in {
            bridge_errors.EXIT_VALIDATION_REFUSED,
            bridge_errors.EXIT_AUTHORITY_REFUSED,
        }
        assert outcome.response["error"]["code"] in {
            bridge_errors.NO_MATERIAL_CHANGE,
            bridge_errors.UNSUPPORTED_EDIT,
        }


class TestWorkspaceKeyLifecycle:
    def test_workspace_creation_generates_private_key(self, tmp_path: Path) -> None:
        workspace = support.create_bridge_workspace(tmp_path, name="keygen")
        path = key_path(workspace)
        assert path.exists()
        st = os.lstat(path)
        assert (st.st_mode & 0o777) == 0o600
        assert st.st_size == 32

    def test_recovery_verifies_key_and_refuses_loss(self, tmp_path: Path) -> None:
        workspace = support.create_bridge_workspace(tmp_path, name="recovery")
        manifest = support.parse_runner_manifest_helper()
        recovered = recover_runner_workspace(str(workspace.workspace_path), manifest)
        assert recovered.callback_key_path.endswith("callback_signing.key")
        assert verify_callback_signing_key(recovered.runtime_path) == key_path(workspace)

        key_path(workspace).unlink()
        with pytest.raises(RunnerWorkspaceError):
            recover_runner_workspace(str(workspace.workspace_path), manifest)

    def test_recovered_workspace_still_verifies_decisions(self, tmp_path: Path) -> None:
        workspace = support.create_bridge_workspace(tmp_path, name="recovered")
        capture = setup_text_proposal(workspace, "snack 9", "recover-1")
        review = get_review(workspace, capture["proposal_public_id"])
        # Simulate a gateway restart: recovery re-verifies the workspace key.
        manifest = support.parse_runner_manifest_helper()
        recover_runner_workspace(str(workspace.workspace_path), manifest)
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK


class TestDecisionSeparationFromFinalization:
    def test_no_command_creates_final_state_across_full_lifecycle(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = setup_text_proposal(workspace, "full lifecycle 10", "sep-1")
        review = get_review(workspace, capture["proposal_public_id"])

        edit_arguments = decision_arguments(workspace, review, "edit")
        edit_arguments["field_updates"] = {"merchant": "Lifecycle Store"}
        edit = support.run_cli(
            support.make_request(
                "edit", edit_arguments, idempotency_key=decision_key("edit", review)
            )
        )
        assert edit.exit_code == bridge_errors.EXIT_OK

        new_review = get_review(workspace, capture["proposal_public_id"])
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, new_review, "confirm"),
                idempotency_key=decision_key("confirm", new_review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK

        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

    def test_receipt_lifecycle_never_finalizes(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        propose = setup_receipt_proposal(workspace, monkeypatch, "sep-2")
        review = get_review(workspace, propose["proposal_public_id"])
        confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review, "confirm"),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert confirm.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
            conversion_status = conn.execute(
                "SELECT COUNT(*) FROM parser_proposal_conversion_audit"
            ).fetchone()[0]
            assert conversion_status == 0
        finally:
            conn.close()


class TestReviewerBoundRegressions:
    """Regressions bound by the three specialist reviews of PR #263."""

    def test_non_ascii_token_is_refused_with_stable_code(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "bus 2", "token-charset")
        review = get_review(workspace, capture["proposal_public_id"])
        outcome = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(
                    workspace, review, "confirm", token_override="fcb_v1_" + "\u00e9" * 32
                ),
                idempotency_key=decision_key("confirm", review),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.CALLBACK_TOKEN_INVALID
        assert outcome.response["error"]["retryable"] is False

    def test_crash_after_decision_commit_replays_original(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finance_core.openclaw_staging_bridge import commands as bridge_commands

        capture = setup_text_proposal(workspace, "train 5", "crash-decision")
        review = get_review(workspace, capture["proposal_public_id"])
        request = support.make_request(
            "confirm",
            decision_arguments(workspace, review, "confirm"),
            idempotency_key=decision_key("confirm", review),
        )

        real_confirm = bridge_commands.confirm_proposal

        def crash_after_commit(conn: object, parser_output_id: int, **kwargs: object) -> dict:
            real_confirm(conn, parser_output_id, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected crash after decision commit")

        monkeypatch.setattr(bridge_commands, "confirm_proposal", crash_after_commit)
        crashed = support.run_cli(request)
        assert crashed.exit_code == bridge_errors.EXIT_INTERNAL
        monkeypatch.undo()

        replay = support.run_cli(request)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True
        assert replay.response["result"]["decision"] == "confirmed"
        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0]
                == 1
            )
            assert all(count == 0 for count in support.count_final_facts(conn).values())
        finally:
            conn.close()

    def test_edit_replay_with_different_actor_fails_closed(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "groceries 40", "edit-actor")
        review = get_review(workspace, capture["proposal_public_id"])
        edit_args = decision_arguments(workspace, review, "edit")
        edit_args["field_updates"] = {"merchant": "FairPrice"}
        first = support.run_cli(
            support.make_request("edit", edit_args, idempotency_key=decision_key("edit", review))
        )
        assert first.exit_code == bridge_errors.EXIT_OK

        # Same idempotency key and material, different actor: the persisted
        # completion row carries the original actor, so replay must conflict.
        replay_args = decision_arguments(workspace, review, "edit")
        replay_args["field_updates"] = {"merchant": "FairPrice"}
        replay_args["operator_actor_id"] = "person_other"
        outcome = support.run_cli(
            support.make_request("edit", replay_args, idempotency_key=decision_key("edit", review))
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT


class TestConsecutiveEdits:
    """Consecutive edit versions carry distinct idempotency identities.

    The edit canonical key binds proposal + pre-edit version + pre-edit
    content hash, so every version appends its own completion row, an
    identical redelivery of one version replays, and fresh review tokens
    authorize the next version.
    """

    def test_consecutive_completion_edits_append_distinct_versions(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = setup_text_proposal(workspace, "supplies 60", "edit-seq")
        review_v0 = get_review(workspace, capture["proposal_public_id"])

        # Edit v0 -> v1.
        arguments_v0 = decision_arguments(workspace, review_v0, "edit")
        arguments_v0["field_updates"] = {"merchant": "First Store"}
        request_v0 = support.make_request(
            "edit", arguments_v0, idempotency_key=decision_key("edit", review_v0)
        )
        first = support.run_cli(request_v0)
        assert first.exit_code == bridge_errors.EXIT_OK, first.response
        assert first.response["result"]["proposal_version"] == 1

        # Identical redelivery of the v0 edit replays safely.
        replay_v0 = support.run_cli(request_v0)
        assert replay_v0.exit_code == bridge_errors.EXIT_OK
        assert replay_v0.response["idempotent_replay"] is True
        assert replay_v0.response["result"]["proposal_version"] == 1

        # Fresh review issues v1 tokens; the next legitimate edit succeeds
        # under its own canonical identity.
        review_v1 = get_review(workspace, capture["proposal_public_id"])
        assert review_v1["proposal_version"] == 1
        assert review_v1["effective_content_hash"] != review_v0["effective_content_hash"]
        arguments_v1 = decision_arguments(workspace, review_v1, "edit")
        arguments_v1["field_updates"] = {"merchant": "Second Store"}
        second = support.run_cli(
            support.make_request(
                "edit", arguments_v1, idempotency_key=decision_key("edit", review_v1)
            )
        )
        assert second.exit_code == bridge_errors.EXIT_OK, second.response
        assert second.response["result"]["proposal_version"] == 2

        review_v2 = get_review(workspace, capture["proposal_public_id"])
        assert review_v2["proposal_version"] == 2

        # Both edits are append-only completion rows with increasing versions.
        conn = support.open_database(workspace)
        try:
            rows = conn.execute(
                "SELECT version_number, base_content_hash FROM parser_proposal_completions "
                "ORDER BY version_number ASC"
            ).fetchall()
            assert [int(row["version_number"]) for row in rows] == [1, 2]
            assert str(rows[0]["base_content_hash"]) == review_v0["effective_content_hash"]
            assert str(rows[1]["base_content_hash"]) == review_v1["effective_content_hash"]
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

        # The v0 replay still resolves after v2 exists (version-safe replay).
        replay_v0_again = support.run_cli(request_v0)
        assert replay_v0_again.exit_code == bridge_errors.EXIT_OK
        assert replay_v0_again.response["idempotent_replay"] is True
        assert replay_v0_again.response["result"]["proposal_version"] == 1

        # The pre-edit v0 confirm token is stale: it cannot confirm the
        # current v2 content.
        stale_confirm = support.run_cli(
            support.make_request(
                "confirm",
                decision_arguments(workspace, review_v0, "confirm"),
                idempotency_key=decision_key("confirm", review_v0),
            )
        )
        assert stale_confirm.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert stale_confirm.response["error"]["code"] == bridge_errors.STALE_VERSION

    def test_consecutive_receipt_monetary_corrections_supersede_in_sequence(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        propose = setup_receipt_proposal(workspace, monkeypatch, "edit-seq-receipt")
        review_v0 = get_review(workspace, propose["proposal_public_id"])

        # First monetary correction: parent -> replacement R1.
        arguments_v0 = decision_arguments(workspace, review_v0, "edit")
        arguments_v0["field_updates"] = {"amount": "50.00", "currency": "SGD"}
        request_v0 = support.make_request(
            "edit", arguments_v0, idempotency_key=decision_key("edit", review_v0)
        )
        first = support.run_cli(request_v0)
        assert first.exit_code == bridge_errors.EXIT_OK, first.response
        r1_public_id = first.response["result"]["proposal_public_id"]
        assert (
            first.response["result"]["superseded_proposal_public_id"]
            == propose["proposal_public_id"]
        )

        # Identical redelivery replays the persisted correction.
        replay_v0 = support.run_cli(request_v0)
        assert replay_v0.exit_code == bridge_errors.EXIT_OK
        assert replay_v0.response["idempotent_replay"] is True
        assert replay_v0.response["result"]["proposal_public_id"] == r1_public_id

        # Second monetary correction on the replacement: R1 -> R2.
        review_r1 = get_review(workspace, r1_public_id)
        arguments_v1 = decision_arguments(workspace, review_r1, "edit")
        arguments_v1["field_updates"] = {"amount": "55.00", "currency": "SGD"}
        second = support.run_cli(
            support.make_request(
                "edit", arguments_v1, idempotency_key=decision_key("edit", review_r1)
            )
        )
        assert second.exit_code == bridge_errors.EXIT_OK, second.response
        r2_public_id = second.response["result"]["proposal_public_id"]
        assert r2_public_id != r1_public_id
        assert second.response["result"]["superseded_proposal_public_id"] == r1_public_id

        conn = support.open_database(workspace)
        try:
            revisions = conn.execute(
                "SELECT correction_public_id FROM receipt_proposal_revisions ORDER BY id ASC"
            ).fetchall()
            assert len(revisions) == 2
            statuses = {
                row["public_id"]: row["parse_status"]
                for row in conn.execute(
                    "SELECT public_id, parse_status FROM parser_outputs"
                ).fetchall()
            }
            assert statuses[propose["proposal_public_id"]] == "superseded"
            assert statuses[r1_public_id] == "superseded"
            assert statuses[r2_public_id] == "parsed_pending_confirmation"
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

        # The first correction still replays to R1 after the second exists.
        replay_v0_again = support.run_cli(request_v0)
        assert replay_v0_again.exit_code == bridge_errors.EXIT_OK
        assert replay_v0_again.response["idempotent_replay"] is True
        assert replay_v0_again.response["result"]["proposal_public_id"] == r1_public_id
