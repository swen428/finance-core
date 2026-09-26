"""S2 contract tests: propose and get_review through the bridge CLI.

Covers text proposal reuse without duplication, receipt OCR-to-proposal
ingestion with a deterministic fake engine, OCR/engine failure with original
evidence preserved and no proposal falsely claimed, idempotent propose
replay, and the bounded get_review card payload including sensitive-field
exclusion.  All data is temporary and synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import openclaw_staging_bridge_d2_posting_cases_v1 as d2_posting_cases
import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.receipt_ocr_evidence import (
    OcrEngineLaunchError,
    ReceiptOcrExtractionStatus,
)
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import ocr_boundary


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def test_d2_raw_delivery_fields_without_host_consumer_proof_cannot_activate(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d2_posting_cases.test_raw_delivery_fields_without_host_consumer_proof_cannot_activate(
        workspace, monkeypatch
    )


def test_d2_delivery_receipt_proof_rejects_tamper_rotation_and_workspace_transplant(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    d2_posting_cases.test_delivery_receipt_proof_rejects_tamper_rotation_and_workspace_transplant(
        workspace, monkeypatch, tmp_path
    )


def test_d2_one_confirm_posts_once_and_status_recovers_same_result(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d2_posting_cases.test_one_confirm_posts_once_and_status_recovers_same_result(
        workspace, monkeypatch
    )


def test_d2_personal_total_receipt_uses_python_card_and_posts_once(
    workspace: support.BridgeWorkspace,
) -> None:
    d2_posting_cases.test_personal_total_receipt_uses_python_card_and_posts_once(workspace)


def test_d2_posting_status_reference_is_context_bound(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d2_posting_cases.test_posting_status_reference_is_context_bound(workspace, monkeypatch)


def capture_text(workspace: support.BridgeWorkspace, text: str, key: str) -> dict:
    update = support.telegram_text_update(text)
    outcome = support.run_cli(
        support.make_request(
            "capture",
            support.authenticated_text_capture_arguments(workspace, update),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert outcome.exit_code == bridge_errors.EXIT_OK
    capture_result = outcome.response["result"]
    assert capture_result["proposal_public_id"] is None
    processed = support.process_captured_text(workspace, outcome)
    assert processed.exit_code == bridge_errors.EXIT_OK, processed.response
    capture_job = processed.response["result"]["capture_job"]
    assert capture_job["proposal_public_id"] is not None
    return {
        **capture_result,
        "capture_job": capture_job,
        "proposal_public_id": capture_job["proposal_public_id"],
    }


def capture_receipt(
    workspace: support.BridgeWorkspace,
    key: str,
    *,
    content: bytes = support.JPEG_BYTES,
    filename: str = "receipt.jpg",
) -> dict:
    support.write_handoff_file(workspace, filename, content)
    outcome = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename=filename),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert outcome.exit_code == bridge_errors.EXIT_OK
    return outcome.response["result"]


class TestProposeText:
    def test_propose_reuses_existing_parser_proposal(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = capture_text(workspace, "brunch 20.00", "propose-text-1")
        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert outcome.response["idempotent_replay"] is True
        assert result["proposal_public_id"] == capture["proposal_public_id"]
        assert result["proposal_version"] == 0
        assert len(result["effective_content_hash"]) == 64
        assert result["parse_status"] == "parsed_pending_confirmation"
        assert result["final_transaction_created"] is False

        conn = support.open_database(workspace)
        try:
            proposal_count = conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0]
            assert proposal_count == 1
        finally:
            conn.close()

    def test_repeated_propose_never_duplicates(self, workspace: support.BridgeWorkspace) -> None:
        capture = capture_text(workspace, "snack 3.50", "propose-text-2")
        for index in range(3):
            outcome = support.run_cli(
                support.make_request(
                    "propose",
                    {
                        "workspace_path": str(workspace.workspace_path),
                        "intake_public_id": capture["intake_public_id"],
                    },
                    idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
                )
            )
            assert outcome.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
        finally:
            conn.close()

    def test_propose_unknown_intake_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": "raw_intake_missing",
                },
                idempotency_key=support.canonical_propose_key("raw_intake_missing"),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.INTAKE_NOT_FOUND


class TestProposeReceipt:
    def test_receipt_propose_ingests_total_level_proposal(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = capture_receipt(workspace, "propose-receipt-1")
        engine = support.FakeOcrEngine()
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)

        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        result = outcome.response["result"]
        assert result["proposal_public_id"].startswith("prop_bridge_")
        assert result["extraction_public_id"].startswith("rocr_bridge_")
        assert result["parse_status"] == "parsed_pending_confirmation"
        assert result["proposal_version"] == 0
        assert len(result["effective_content_hash"]) == 64
        assert result["final_transaction_created"] is False
        assert engine.calls == 1

        conn = support.open_database(workspace)
        try:
            # One proposal, one OCR extraction, one link; nothing final.
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1
            )
            facts_before = support.count_final_facts(conn)
            assert all(count == 0 for count in facts_before.values())
        finally:
            conn.close()

    def test_receipt_propose_replay_is_idempotent(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = capture_receipt(workspace, "propose-receipt-2")
        engine = support.FakeOcrEngine()
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
        request = support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": capture["intake_public_id"],
            },
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
        # The second call replays through the existing persisted proposal.
        assert second.response["idempotent_replay"] is True

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        finally:
            conn.close()

    def test_ocr_engine_failure_preserves_evidence_without_proposal(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = capture_receipt(workspace, "propose-receipt-3")
        failing = support.FakeOcrEngine(
            raise_on_extract=OcrEngineLaunchError("injected engine failure")
        )
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: failing)

        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.OCR_EXTRACTION_FAILED
        assert outcome.response["error"]["retryable"] is False

        conn = support.open_database(workspace)
        try:
            # Original intake and attachment evidence survive; no proposal, no facts.
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

        # Recovery: a working engine completes the proposal on retry.
        recovered = support.FakeOcrEngine()
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: recovered)
        retry = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert retry.exit_code == bridge_errors.EXIT_OK

    def test_no_text_ocr_outcome_still_stops_at_pending(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = capture_receipt(workspace, "propose-receipt-4")
        engine = support.FakeOcrEngine(
            blocks=(), status=ReceiptOcrExtractionStatus.NO_TEXT, outcome_code="no_text"
        )
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        # The deterministic receipt parser may refuse unusable evidence; either
        # way no final fact may appear and no success is falsely claimed.
        if outcome.exit_code == bridge_errors.EXIT_OK:
            assert outcome.response["result"]["parse_status"] == "parsed_pending_confirmation"
        else:
            assert outcome.response["status"] == "error"
        conn = support.open_database(workspace)
        try:
            facts = support.count_final_facts(conn)
            assert all(count == 0 for count in facts.values())
        finally:
            conn.close()

    def test_propose_receipt_without_capture_is_refused(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A manual non-bridge intake with no attachment evidence cannot propose.
        conn = support.open_database(workspace)
        try:
            from finance_core.intake.raw_text_repository import create_raw_intake_record

            with conn:
                record = create_raw_intake_record(
                    conn,
                    "manual note",
                    source_type="manual_entry",
                    source_channel="manual",
                    public_id="raw_intake_manual_1",
                )
            intake_public_id = record["public_id"]
        finally:
            conn.close()

        outcome = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": intake_public_id,
                },
                idempotency_key=support.canonical_propose_key(intake_public_id),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE


class TestGetReview:
    def test_review_card_is_bounded_and_excludes_sensitive_material(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        capture = capture_text(workspace, "taxi to airport 35.50", "review-text-1")
        conn = support.open_database(workspace)
        try:
            row = conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
                (capture["proposal_public_id"],),
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["category"] = "transport"
            conn.execute(
                "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
                (json.dumps(payload), capture["proposal_public_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": capture["proposal_public_id"],
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        card = outcome.response["result"]
        assert card["proposal_public_id"] == capture["proposal_public_id"]
        assert card["parse_status"] == "parsed_pending_confirmation"
        assert card["proposal_version"] == 0
        assert len(card["effective_content_hash"]) == 64
        assert card["amount"] == "35.50"
        assert card["currency"] is None
        assert card["transaction_date"] is None
        assert card["merchant"] == "taxi"
        assert card["category"] == "transport"
        assert card["account"] is None
        assert card["account_status"] in {"present", "absent"}
        assert card["classification"] in {"personal", "shared"}
        assert card["source_type"] == "telegram_text"
        assert isinstance(card["ambiguity_indicators"], list)
        assert "missing_currency" in card["ambiguity_indicators"]
        assert "missing_date" in card["ambiguity_indicators"]
        assert card["final_transaction_created"] is False

        tokens = card["callback_tokens"]
        assert set(tokens) == {"confirm", "edit", "reject"}
        for action, entry in tokens.items():
            assert entry["token"].startswith("fcb_v1_")
            assert isinstance(entry["expiry"], int)
        assert len({tokens[a]["token"] for a in tokens}) == 3

        rendered = json.dumps(outcome.response)
        support.assert_no_sensitive_material(rendered, workspace)
        assert "password" not in rendered.lower()

    def test_review_unknown_proposal_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": "parser_output_missing",
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_NOT_FOUND

    def test_review_ttl_bounds_are_enforced(self, workspace: support.BridgeWorkspace) -> None:
        capture = capture_text(workspace, "bus 2.00", "review-text-2")
        for bad_ttl in (0, 59, 86_401, "long"):
            outcome = support.run_cli(
                support.make_request(
                    "get_review",
                    {
                        "workspace_path": str(workspace.workspace_path),
                        "proposal_public_id": capture["proposal_public_id"],
                        "token_ttl_seconds": bad_ttl,
                    },
                )
            )
            assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED

    def test_receipt_review_reports_ocr_ambiguity(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = capture_receipt(workspace, "review-receipt-1")
        engine = support.FakeOcrEngine()
        monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
        propose = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert propose.exit_code == bridge_errors.EXIT_OK

        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": propose.response["result"]["proposal_public_id"],
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        card = outcome.response["result"]
        assert card["source_type"] == "telegram_image"
        assert isinstance(card["ambiguity_indicators"], list)
        conn = support.open_database(workspace)
        try:
            row = conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
                (propose.response["result"]["proposal_public_id"],),
            ).fetchone()
            persisted_flags = json.loads(row["parsed_payload"])["ambiguity_flags"]
        finally:
            conn.close()
        assert persisted_flags
        assert set(persisted_flags).issubset(card["ambiguity_indicators"])
        rendered = json.dumps(outcome.response)
        support.assert_no_sensitive_material(rendered, workspace)

    @pytest.mark.parametrize(
        "ambiguity_flags",
        [
            None,
            "currency_not_determined",
            [7],
            ["unknown_receipt_flag"],
            ["ocr_no_text", "ocr_no_text"],
        ],
    )
    def test_receipt_review_refuses_malformed_authoritative_ambiguity_flags(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
        ambiguity_flags: object,
    ) -> None:
        capture = capture_receipt(workspace, "review-receipt-malformed-flags")
        monkeypatch.setattr(
            ocr_boundary,
            "engine_factory",
            lambda _workspace: support.FakeOcrEngine(),
        )
        propose = support.run_cli(
            support.make_request(
                "propose",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": capture["intake_public_id"],
                },
                idempotency_key=support.canonical_propose_key(capture["intake_public_id"]),
            )
        )
        assert propose.exit_code == bridge_errors.EXIT_OK
        conn = support.open_database(workspace)
        try:
            row = conn.execute(
                "SELECT id, parsed_payload FROM parser_outputs WHERE public_id = ?",
                (propose.response["result"]["proposal_public_id"],),
            ).fetchone()
            payload = json.loads(row["parsed_payload"])
            payload["ambiguity_flags"] = ambiguity_flags
            rendered = json.dumps(payload)
            conn.execute(
                "UPDATE parser_outputs SET parsed_payload = ?, normalized_payload = ? WHERE id = ?",
                (rendered, rendered, row["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": propose.response["result"]["proposal_public_id"],
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    def test_callback_key_loss_fails_closed_without_regeneration(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        # get_review is strictly read-only: a lost callback key must fail
        # closed, never create or repair the key, and no tokens are issued.
        capture = capture_text(workspace, "metro 5.00", "review-text-3")
        key_path = workspace.workspace_path / "runtime" / "callback_signing.key"
        key_path.unlink()

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
        assert outcome.response["error"]["retryable"] is False
        assert not key_path.exists()


class TestReviewCardFieldBounds:
    """Reviewer-bound regressions for bounded review-card field rendering."""

    @staticmethod
    def _inflate_payload(
        workspace: support.BridgeWorkspace,
        *,
        amount: object | None = None,
        currency: object | None = None,
        transaction_date: object | None = None,
        merchant: object | None = None,
        description: object | None = None,
        account: object | None = None,
        account_id: object | None = None,
    ) -> str:
        capture = capture_text(workspace, "lunch 12.50", "bounds-capture")
        conn = support.open_database(workspace)
        try:
            row = conn.execute("SELECT id, parsed_payload FROM parser_outputs").fetchone()
            payload = json.loads(row["parsed_payload"])
            if amount is not None:
                payload["amount"] = amount
            if currency is not None:
                payload["currency"] = currency
            if transaction_date is not None:
                payload["transaction_date"] = transaction_date
            if merchant is not None:
                payload["merchant"] = merchant
            if description is not None:
                payload["description"] = description
            if account is not None:
                payload["account"] = account
            if account_id is not None:
                payload["account_id"] = account_id
            rendered = json.dumps(payload)
            conn.execute(
                "UPDATE parser_outputs SET parsed_payload = ?, normalized_payload = ? WHERE id = ?",
                (rendered, rendered, row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return capture["proposal_public_id"]

    def test_oversized_display_field_truncates_with_indicator(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, merchant="M" * 2_000)
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
        result = outcome.response["result"]
        assert len(result["merchant"]) == 1_024
        assert "oversized_display_field" in result["ambiguity_indicators"]
        support.assert_no_sensitive_material(json.dumps(outcome.response), workspace)

    def test_oversized_monetary_field_refuses_the_card(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, amount="9" * 2_000)
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("merchant", True),
            ("merchant", 7),
            ("merchant", {"name": "Cafe"}),
            ("description", False),
            ("description", ["lunch"]),
        ],
    )
    def test_non_string_merchant_or_description_refuses_instead_of_coercing(
        self,
        workspace: support.BridgeWorkspace,
        field: str,
        value: object,
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, **{field: value})
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount", "   "),
            ("currency", "\u00a0"),
            ("transaction_date", "\t"),
            ("merchant", "   "),
            ("description", "\u00a0"),
            ("account", ""),
            ("account_id", "   "),
        ],
    )
    def test_blank_hash_bound_review_field_refuses_hidden_content(
        self,
        workspace: support.BridgeWorkspace,
        field: str,
        value: str,
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, **{field: value})
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    def test_account_is_returned_exactly_for_informed_confirmation(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, account="travel-wallet")
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
        result = outcome.response["result"]
        assert result["account_status"] == "present"
        assert result["account"] == "travel-wallet"

    def test_oversized_account_is_flagged_before_active_card_rendering(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, account="A" * 2_000)
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
        result = outcome.response["result"]
        assert result["account_status"] == "present"
        assert len(result["account"]) == 1_024
        assert "oversized_display_field" in result["ambiguity_indicators"]

    def test_account_id_is_used_when_account_is_missing(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, account_id="wallet-42")
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
        result = outcome.response["result"]
        assert result["account_status"] == "present"
        assert result["account"] == "wallet-42"

    @pytest.mark.parametrize("account", [True, 7, {"id": "travel-wallet"}])
    def test_non_string_account_refuses_instead_of_coercing(
        self, workspace: support.BridgeWorkspace, account: object
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, account=account)
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    @pytest.mark.parametrize("amount", [35.5, True, {"value": "35.50"}])
    def test_non_string_monetary_field_refuses_instead_of_coercing(
        self, workspace: support.BridgeWorkspace, amount: object
    ) -> None:
        proposal_public_id = self._inflate_payload(workspace, amount=amount)
        outcome = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.PROPOSAL_UNAVAILABLE
        assert outcome.response["error"]["retryable"] is False

    def test_oversized_response_falls_back_to_stable_error(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finance_core.openclaw_staging_bridge import commands as bridge_commands
        from finance_core.openclaw_staging_bridge import envelope as bridge_envelope

        monkeypatch.setattr(
            bridge_commands,
            "handle_health",
            lambda request, deadline: (
                {"blob": "x" * bridge_envelope.MAX_RESPONSE_BYTES},
                False,
            ),
        )
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace))
        )
        assert outcome.exit_code == bridge_errors.EXIT_INTERNAL
        assert outcome.response["error"]["code"] == bridge_errors.RESPONSE_TOO_LARGE
        assert outcome.response["error"]["retryable"] is False
