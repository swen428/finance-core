"""S1C-B managed posting and finalization Bridge proofs.

These cases exercise only public Bridge requests against disposable synthetic
managed profiles.  Shared profile setup and the closed-session witness come
from the S1C-A fixture.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core import posting_authority
from finance_core.intake.attachment_evidence import persist_attachment_evidence
from finance_core.intake.raw_text_repository import (
    TELEGRAM_PHOTO_FINGERPRINT_VERSION,
    create_raw_intake_record,
)
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.intake.receipt_ocr_proposal import (
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.openclaw_staging_bridge import (
    commands,
    delivery_receipt_cli,
    envelope,
    errors,
    workspace_access,
)
from finance_core.openclaw_staging_bridge.delivery_receipt_proof import (
    PROOF_VERSION,
    receipt_proof_sha256,
)
from finance_core.parser_proposals.confirmation import confirm_proposal
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.participants import bootstrap_participants
from finance_core.receipt_staging_runner.workspace import (
    generate_delivery_receipt_signing_key,
    load_delivery_receipt_signing_key,
)
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    record_telegram_source_context,
)
from tests import test_openclaw_staging_bridge_finalize_status_v1 as s4
from tests import test_receipt_ocr_proposal_ingestion_v1 as receipt_ocr_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)


def _prepare_initial_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    proposal_id: str,
    *,
    context: dict[str, object] | None = None,
    admitted_message_id: str = s1ca.SEEDED_MESSAGE_ID,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        **(context if context is not None else s1ca._context(workspace)),
        "proposal_public_id": proposal_id,
        "admitted_source_message_id": admitted_message_id,
    }
    return s1ca._request(
        workspace,
        envelope.COMMAND_PREPARE_POSTING_REVIEW,
        arguments,
        idempotency_key=commands.canonical_prepare_initial_posting_review_key(
            proposal_id, admitted_message_id
        ),
    )


def _domain_snapshot(workspace: s1ca.ManagedBridgeWorkspace) -> tuple[tuple[str, int], ...]:
    """Count affected synthetic domain rows without comparing SQLite bytes."""
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1c-b-domain-snapshot"
    ) as conn:
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        included = sorted(
            name
            for name in table_names
            if name.startswith("d2_posting_")
            or name
            in {
                "parser_proposal_confirmations",
                "parser_proposal_completions",
                "financial_audit_events",
                "transactions",
                "receipt_item_allocation_fact_sets",
                "receipt_item_allocation_facts",
                "authoritative_calculation_snapshots",
                "receipt_finalization_authorizations",
                "receipt_finalization_audit",
            }
        )
        return tuple(
            (name, int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]))
            for name in included
        )


def _assert_refused_without_leak_or_writes(
    workspace: s1ca.ManagedBridgeWorkspace,
    request: dict[str, object],
    *,
    private_values: tuple[str, ...],
) -> support.CliOutcome:
    before = _domain_snapshot(workspace)
    before_sessions = len(workspace.sessions)
    outcome = s1ca._run(workspace, request)
    assert outcome.exit_code == errors.EXIT_AUTHORITY_REFUSED, outcome.response
    assert outcome.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    assert "result" not in outcome.response
    response_text = json.dumps(outcome.response, sort_keys=True)
    for private_value in private_values:
        assert private_value not in response_text
    assert _domain_snapshot(workspace) == before
    assert workspace.sessions[before_sessions].total_changes == 0
    return outcome


def _signed_delivery_receipt(
    workspace: s1ca.ManagedBridgeWorkspace,
    manifest: dict[str, object],
    *,
    provider_message_id: str,
) -> dict[str, object]:
    fields = {
        "workspace_path": str(workspace.workspace_path),
        "attempt_nonce": manifest["delivery_attempt_nonce"],
        "capability": posting_authority.DELIVERY_MATERIAL_CAPABILITY,
        "delivery_material_version": posting_authority.DELIVERY_MATERIAL_VERSION,
        "delivery_material_sha256": manifest["finance_delivery_material_sha256"],
        "provider_message_id": provider_message_id,
        "receipt_token_sha256": hashlib.sha256(
            f"synthetic-delivery-{provider_message_id}".encode()
        ).hexdigest(),
        "channel": "telegram",
        "account_id": s1ca.ACCOUNT,
        "conversation_id": s1ca.CONVERSATION,
        "session_key": s1ca.BINDING,
        "source_identity_sha256": "c" * 64,
    }
    signing_key = load_delivery_receipt_signing_key(str(workspace.workspace_path / "runtime"))
    return {
        **fields,
        "receipt_proof_version": PROOF_VERSION,
        "receipt_proof_sha256": receipt_proof_sha256(signing_key=signing_key, **fields),
    }


def _consume_delivery_receipt(
    workspace: s1ca.ManagedBridgeWorkspace,
    manifest: dict[str, object],
) -> None:
    payload = _signed_delivery_receipt(
        workspace,
        manifest,
        provider_message_id="930",
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    before_sessions = len(workspace.sessions)
    exit_code = delivery_receipt_cli.execute_stream(
        io.BytesIO(json.dumps(payload).encode()), stdout, stderr
    )
    assert exit_code == errors.EXIT_OK, stderr.getvalue()
    assert json.loads(stdout.getvalue())["status"] == "ok"
    opened = workspace.sessions[before_sessions:]
    assert len(opened) == 1
    for observation in opened:
        assert observation.workspace_path == workspace.workspace_path
        with pytest.raises(sqlite3.ProgrammingError):
            observation.connection.execute("SELECT 1")


def _prepare_card_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    card_id: str,
) -> dict[str, object]:
    return s1ca._request(
        workspace,
        envelope.COMMAND_PREPARE_POSTING_REVIEW,
        {**s1ca._context(workspace), "card_generation_public_id": card_id},
        idempotency_key=commands.canonical_prepare_posting_review_key(card_id),
    )


def _same_key_wrong_source_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    request: dict[str, object],
    *,
    account_id: str | None = None,
    binding_id: str | None = None,
) -> dict[str, object]:
    arguments = dict(request["arguments"])
    if account_id is not None:
        arguments["telegram_account_id"] = account_id
    if binding_id is not None:
        arguments["conversation_binding_id"] = binding_id
    return s1ca._request(
        workspace,
        str(request["command"]),
        arguments,
        idempotency_key=str(request["idempotency_key"]),
    )


def _assert_wrong_account_and_binding_replays_refused(
    workspace: s1ca.ManagedBridgeWorkspace,
    request: dict[str, object],
    *,
    private_values: tuple[str, ...],
) -> None:
    for change in (
        {"account_id": "different-managed-account"},
        {"binding_id": "different-managed-binding"},
    ):
        _assert_refused_without_leak_or_writes(
            workspace,
            _same_key_wrong_source_request(workspace, request, **change),
            private_values=private_values,
        )


def _card_for_proposal(
    workspace: s1ca.ManagedBridgeWorkspace,
    proposal_id: str,
) -> str:
    row = s1ca._read_one(
        workspace,
        """
        SELECT cards.card_generation_public_id
        FROM parser_human_draft_publications AS publications
        JOIN parser_human_draft_cards AS cards
          ON cards.draft_id = publications.draft_id
         AND cards.parser_output_id = publications.parser_output_id
        WHERE publications.proposal_public_id = ?
        """,
        (proposal_id,),
    )
    assert row is not None
    return str(row[0])


def _seed_managed_receipt_proposal(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    confirm: bool = True,
) -> tuple[str, int, str]:
    """Seed a real synthetic OCR/source/confirmation chain with domain APIs."""
    receipt_bytes = b"\xff\xd8\xffs1cb-synthetic-receipt-image"
    attachment_hash = hashlib.sha256(receipt_bytes).hexdigest()
    attachment_path = workspace.workspace_path / "attachments" / "s1cb-receipt.jpg"
    attachment_path.write_bytes(receipt_bytes)
    attachment_path.chmod(0o400)

    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-seed-s1c-b-receipt"
    ) as conn:
        bootstrap_participants(conn, parse_runner_manifest(support.make_manifest_bytes()))
        intake = create_raw_intake_record(
            conn,
            "synthetic receipt image",
            source_type="telegram_image",
            source_channel="telegram",
            source_metadata={
                "chat_id": s1ca.CONVERSATION,
                "message_id": "141",
                "source_received_at": "2026-09-24T00:00:00Z",
                "attachment_hash": attachment_hash,
            },
            received_at="2026-09-24T00:00:00Z",
            public_id="raw_intake_s1cb_receipt",
            fingerprint_version=TELEGRAM_PHOTO_FINGERPRINT_VERSION,
        )
        record_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=s1ca.ACTOR,
                account_id=s1ca.ACCOUNT,
                conversation_id=s1ca.CONVERSATION,
                binding_id=s1ca.BINDING,
                message_id="141",
            ),
            captured_at="2026-09-24T00:00:00Z",
        )
        conn.commit()

        attachment = persist_attachment_evidence(
            conn,
            attachment_path,
            public_id="tgae_s1cb_receipt",
            raw_intake_id=int(intake["id"]),
            telegram_file_id="file_s1cb_receipt",
            telegram_file_unique_id="unique_s1cb_receipt",
            original_filename="receipt.jpg",
            declared_mime_type="image/jpeg",
            expected_file_size=len(receipt_bytes),
            expected_content_hash=attachment_hash,
        )
        extraction = extract_and_persist_receipt_ocr_evidence(
            conn,
            public_id="rocr_s1cb_receipt",
            attachment_id=int(attachment["attachment_id"]),
            engine=receipt_ocr_tests.FakeEngine(
                result=ReceiptOcrEngineResult(
                    status=ReceiptOcrExtractionStatus.SUCCEEDED,
                    blocks=receipt_ocr_tests._sgd_blocks(),
                    outcome_code="ok",
                )
            ),
        )
        ingested = ingest_receipt_ocr_evidence_as_total_expense_proposal(
            conn,
            extraction_public_id=extraction.public_id,
            proposal_public_id="prop_s1cb_receipt",
            link_public_id="ropl_s1cb_receipt",
        )
        if confirm:
            confirm_proposal(
                conn,
                ingested.parser_output_id,
                actor=s1ca.ACTOR,
                confirmation_public_id="pca_s1cb_receipt",
                confirmation_channel="synthetic_test_fixture",
            )
        conn.commit()
        proposal = ParserProposalRepository(conn).get_by_public_id("prop_s1cb_receipt")
        assert proposal is not None
        _payload, _completion_id, version = resolve_effective_payload(conn, proposal)
        content_hash = compute_effective_proposal_content_hash(conn, proposal)
        return str(proposal["public_id"]), version, content_hash


def test_managed_initial_prepare_refuses_missing_context_and_unknown_proposal(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    missing_context_request = _prepare_initial_request(
        managed_workspace,
        "prop_s1cb_missing_context",
        context={},
    )
    before = _domain_snapshot(managed_workspace)
    before_sessions = len(managed_workspace.sessions)
    missing_context = s1ca._run(managed_workspace, missing_context_request, expected_sessions=None)
    assert missing_context.exit_code == errors.EXIT_VALIDATION_REFUSED
    assert missing_context.response["error"]["code"] == errors.ARGUMENTS_REFUSED
    assert "result" not in missing_context.response
    assert _domain_snapshot(managed_workspace) == before
    assert all(
        observation.total_changes == 0
        for observation in managed_workspace.sessions[before_sessions:]
    )

    _assert_refused_without_leak_or_writes(
        managed_workspace,
        _prepare_initial_request(managed_workspace, "prop_s1cb_unknown_proposal"),
        private_values=("prop_s1cb_unknown_proposal",),
    )


def test_managed_initial_text_posting_prepare_replays_and_rechecks_source(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    proposal_id = s1ca._seed_proposal(managed_workspace)
    request = _prepare_initial_request(managed_workspace, proposal_id)

    prepared = s1ca._run(managed_workspace, request)
    replay = s1ca._run(managed_workspace, request)

    assert prepared.exit_code == errors.EXIT_OK, prepared.response
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    result = prepared.response["result"]
    assert result["proposal_public_id"] == proposal_id
    assert result["posting_path"] == "text"
    assert result["final_transaction_created"] is False
    assert replay.response["result"]["review_public_id"] == result["review_public_id"]

    wrong_binding = {
        **s1ca._context(managed_workspace),
        "conversation_binding_id": "binding-from-another-session",
    }
    _assert_refused_without_leak_or_writes(
        managed_workspace,
        _prepare_initial_request(managed_workspace, proposal_id, context=wrong_binding),
        private_values=(
            proposal_id,
            str(result["review_public_id"]),
            str(result["proposal_content_hash"]),
        ),
    )


@pytest.mark.parametrize(
    "context_change",
    [
        {"account_id": "different-account"},
        {"binding_id": "different-binding"},
    ],
)
def test_managed_confirmed_text_finalize_replay_rechecks_source(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    context_change: dict[str, str],
) -> None:
    proposal_id = s1ca._seed_proposal(
        managed_workspace,
        source_text="synthetic managed text expense",
        amount="12.50",
    )
    context = s1ca._context(managed_workspace)
    review = s1ca._review(managed_workspace, proposal_id, context=context)
    confirm = s1ca._request(
        managed_workspace,
        envelope.COMMAND_CONFIRM,
        s1ca._decision_arguments(review, "confirm", context=context),
        idempotency_key=s1ca._decision_key("confirm", review),
    )
    confirmed = s1ca._run(managed_workspace, confirm)
    assert confirmed.exit_code == errors.EXIT_OK, confirmed.response

    finalize_arguments: dict[str, object] = {
        **context,
        "proposal_public_id": proposal_id,
        "operator_actor_id": s1ca.ACTOR,
        "proposal_version": review["proposal_version"],
        "content_hash": review["effective_content_hash"],
    }
    finalize = s1ca._request(
        managed_workspace,
        envelope.COMMAND_FINALIZE,
        finalize_arguments,
        idempotency_key=commands.canonical_finalize_key(proposal_id),
    )
    first = s1ca._run(managed_workspace, finalize)
    replay = s1ca._run(managed_workspace, finalize)

    assert first.exit_code == errors.EXIT_OK, first.response
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert first.response["result"]["path"] == "text_expense"
    assert first.response["result"]["final_transaction_created"] is True
    assert replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["transaction_public_id"]
        == first.response["result"]["transaction_public_id"]
    )
    assert dict(_domain_snapshot(managed_workspace))["transactions"] == 1

    wrong_context = {**context, **context_change}
    if "account_id" in wrong_context:
        wrong_context["telegram_account_id"] = wrong_context.pop("account_id")
    if "binding_id" in wrong_context:
        wrong_context["conversation_binding_id"] = wrong_context.pop("binding_id")
    wrong_source_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_FINALIZE,
        {**finalize_arguments, **wrong_context},
        idempotency_key=commands.canonical_finalize_key(proposal_id),
    )
    _assert_refused_without_leak_or_writes(
        managed_workspace,
        wrong_source_request,
        private_values=(
            proposal_id,
            str(first.response["result"]["transaction_public_id"]),
            str(first.response["result"]["content_hash"]),
        ),
    )


@pytest.mark.parametrize(
    "source_kind",
    ("initial_text", "sealed_d1_card", "pending_initial_ocr_receipt"),
)
def test_managed_posting_flow_and_replays_for_initial_and_sealed_sources(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    source_kind: str,
) -> None:
    generate_delivery_receipt_signing_key(str(managed_workspace.workspace_path / "runtime"))
    if source_kind == "initial_text":
        proposal_id = s1ca._seed_proposal(managed_workspace)
        prepare_request = _prepare_initial_request(managed_workspace, proposal_id)
    elif source_kind == "sealed_d1_card":
        proposal_id = s1ca._create_d1_child_proposal(managed_workspace)
        card_id = _card_for_proposal(managed_workspace, proposal_id)
        prepare_request = _prepare_card_request(managed_workspace, card_id)
    else:
        proposal_id, _version, _content_hash = _seed_managed_receipt_proposal(
            managed_workspace, confirm=False
        )
        confirmation_count = s1ca._read_one(
            managed_workspace,
            "SELECT COUNT(*) FROM parser_proposal_confirmations "
            "WHERE parser_output_id = "
            "(SELECT id FROM parser_outputs WHERE public_id = ?)",
            (proposal_id,),
        )
        assert confirmation_count is not None and int(confirmation_count[0]) == 0
        prepare_request = _prepare_initial_request(
            managed_workspace, proposal_id, admitted_message_id="141"
        )

    prepared = s1ca._run(managed_workspace, prepare_request)
    prepare_replay = s1ca._run(managed_workspace, prepare_request)
    assert prepared.exit_code == errors.EXIT_OK, prepared.response
    assert prepare_replay.exit_code == errors.EXIT_OK, prepare_replay.response
    assert prepare_replay.response["idempotent_replay"] is True
    review = prepared.response["result"]
    assert review["proposal_public_id"] == proposal_id
    expected_posting_path = (
        "personal_receipt" if source_kind == "pending_initial_ocr_receipt" else "text"
    )
    assert review["posting_path"] == expected_posting_path
    assert review["final_transaction_created"] is False
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        prepare_request,
        private_values=(
            proposal_id,
            str(review["review_public_id"]),
            str(review["proposal_content_hash"]),
        ),
    )

    review_id = str(review["review_public_id"])
    issue_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_ISSUE_POSTING_REVIEW_ACTIONS,
        {
            **s1ca._context(managed_workspace),
            "posting_review_public_id": review_id,
        },
        idempotency_key=commands.canonical_issue_posting_review_actions_key(review_id),
    )
    issued = s1ca._run(managed_workspace, issue_request)
    issue_replay = s1ca._run(managed_workspace, issue_request)
    assert issued.exit_code == errors.EXIT_OK, issued.response
    assert issue_replay.exit_code == errors.EXIT_OK, issue_replay.response
    assert issue_replay.response["idempotent_replay"] is True
    manifest = issued.response["result"]
    assert manifest["text"] == review["presentation_text"]
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        issue_request,
        private_values=(review_id, str(manifest["delivery_attempt_public_id"])),
    )
    _consume_delivery_receipt(managed_workspace, manifest)

    controls = manifest["controls"]
    control = next(
        item for item in controls if isinstance(item, dict) and item.get("action") == "confirm"
    )
    reference = str(control["callback_value"]).removeprefix("post:")
    callback_id = f"s1c-b-{source_kind}-confirm"
    confirm_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_CONFIRM_AND_POST,
        {
            **s1ca._context(managed_workspace),
            "short_reference": reference,
            "callback_id": callback_id,
            "callback_message_id": 930,
        },
        idempotency_key=commands.canonical_confirm_and_post_key(callback_id),
    )
    confirmed = s1ca._run(managed_workspace, confirm_request)
    confirm_replay = s1ca._run(managed_workspace, confirm_request)
    assert confirmed.exit_code == errors.EXIT_OK, confirmed.response
    assert confirm_replay.exit_code == errors.EXIT_OK, confirm_replay.response
    assert confirm_replay.response["idempotent_replay"] is True
    assert confirmed.response["result"]["state"] == "finalized"
    assert confirmed.response["result"]["final_transaction_created"] is True
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        confirm_request,
        private_values=(
            review_id,
            str(confirmed.response["result"].get("transaction_public_id", "")),
        ),
    )

    attempt_id = str(confirmed.response["result"]["attempt_public_id"])
    resume_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_RESUME_POSTING,
        {
            **s1ca._context(managed_workspace),
            "attempt_public_id": attempt_id,
        },
        idempotency_key=commands.canonical_resume_posting_key(attempt_id),
    )
    resumed = s1ca._run(managed_workspace, resume_request)
    resume_replay = s1ca._run(managed_workspace, resume_request)
    assert resumed.exit_code == errors.EXIT_OK, resumed.response
    assert resume_replay.exit_code == errors.EXIT_OK, resume_replay.response
    assert resumed.response["result"] == resume_replay.response["result"]
    assert resumed.response["result"]["state"] == "finalized"
    assert resumed.response["result"]["final_transaction_created"] is True
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        resume_request,
        private_values=(
            attempt_id,
            str(resumed.response["result"].get("transaction_public_id", "")),
        ),
    )
    assert dict(_domain_snapshot(managed_workspace))["transactions"] == 1


def test_managed_initial_ocr_receipt_manual_completion_five_stage_flow(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id, version, content_hash = _seed_managed_receipt_proposal(managed_workspace)
    context = s1ca._context(managed_workspace)
    proposal_identity = {
        **context,
        "proposal_public_id": proposal_id,
        "proposal_version": version,
        "content_hash": content_hash,
    }
    prepare_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_PREPARE_RECEIPT_COMPLETION,
        proposal_identity,
        idempotency_key=commands.canonical_prepare_receipt_completion_key(proposal_id),
    )
    prepared = s1ca._run(managed_workspace, prepare_request)
    prepare_replay = s1ca._run(managed_workspace, prepare_request)
    assert prepared.exit_code == errors.EXIT_OK, prepared.response
    assert prepare_replay.exit_code == errors.EXIT_OK, prepare_replay.response
    assert prepare_replay.response["idempotent_replay"] is True
    receipt_identities = s4.receipt_identities(
        {"proposal_public_id": proposal_id, "effective_content_hash": content_hash}
    )
    conversion_command_id, receipt_public_id = receipt_identities
    assert prepared.response["result"]["receipt_public_id"] == receipt_public_id
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        prepare_request,
        private_values=(
            proposal_id,
            receipt_public_id,
            str(prepared.response["result"]["conversion_result_hash"]),
        ),
    )

    fact_set_filename = "s1cb-receipt-fact-set.json"
    fact_set_command = s4.build_iaf_command(
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=conversion_command_id,
        conversion_result_hash=str(prepared.response["result"]["conversion_result_hash"]),
        actor=s1ca.ACTOR,
    )
    s4.write_command_file(managed_workspace, fact_set_filename, fact_set_command)

    read_calls: list[str] = []
    real_read_command_file = workspace_access.read_command_file

    def observe_command_file_read(workspace: Path, filename: str) -> bytes:
        read_calls.append(filename)
        return real_read_command_file(workspace, filename)

    monkeypatch.setattr(workspace_access, "read_command_file", observe_command_file_read)
    wrong_binding = {
        **context,
        "conversation_binding_id": "foreign-receipt-binding",
    }
    wrong_apply_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_APPLY_FACT_SET,
        {
            **wrong_binding,
            "proposal_public_id": proposal_id,
            "command_filename": fact_set_filename,
        },
        idempotency_key=commands.canonical_apply_fact_set_key(proposal_id),
    )
    _assert_refused_without_leak_or_writes(
        managed_workspace,
        wrong_apply_request,
        private_values=(proposal_id, receipt_public_id, conversion_command_id),
    )
    assert read_calls == []

    apply_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_APPLY_FACT_SET,
        {
            **context,
            "proposal_public_id": proposal_id,
            "command_filename": fact_set_filename,
        },
        idempotency_key=commands.canonical_apply_fact_set_key(proposal_id),
    )
    applied = s1ca._run(managed_workspace, apply_request)
    apply_replay = s1ca._run(managed_workspace, apply_request)
    assert applied.exit_code == errors.EXIT_OK, applied.response
    assert apply_replay.exit_code == errors.EXIT_OK, apply_replay.response
    assert apply_replay.response["idempotent_replay"] is True
    assert read_calls == [fact_set_filename, fact_set_filename]
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        apply_request,
        private_values=(proposal_id, receipt_public_id, conversion_command_id),
    )
    assert read_calls == [fact_set_filename, fact_set_filename]

    snapshot_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW,
        {
            **context,
            "proposal_public_id": proposal_id,
        },
        idempotency_key=commands.canonical_finalization_snapshot_review_key(proposal_id),
    )
    snapshot = s1ca._run(managed_workspace, snapshot_request)
    snapshot_replay = s1ca._run(managed_workspace, snapshot_request)
    assert snapshot.exit_code == errors.EXIT_OK, snapshot.response
    assert snapshot_replay.exit_code == errors.EXIT_OK, snapshot_replay.response
    assert snapshot_replay.response["idempotent_replay"] is True
    snapshot_result = snapshot.response["result"]
    assert (
        snapshot_result["calculation_snapshot_hash"]
        == snapshot_replay.response["result"]["calculation_snapshot_hash"]
    )
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        snapshot_request,
        private_values=(proposal_id, str(snapshot_result["calculation_snapshot_hash"])),
    )

    authorize_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_AUTHORIZE_FINALIZATION,
        {
            **context,
            "proposal_public_id": proposal_id,
            "expected_calculation_snapshot_hash": snapshot_result["calculation_snapshot_hash"],
        },
        idempotency_key=commands.canonical_authorize_finalization_key(proposal_id),
    )
    authorized = s1ca._run(managed_workspace, authorize_request)
    authorize_replay = s1ca._run(managed_workspace, authorize_request)
    assert authorized.exit_code == errors.EXIT_OK, authorized.response
    assert authorize_replay.exit_code == errors.EXIT_OK, authorize_replay.response
    assert (
        authorized.response["result"]["authorization_id"]
        == authorize_replay.response["result"]["authorization_id"]
    )
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        authorize_request,
        private_values=(
            proposal_id,
            str(authorized.response["result"]["authorization_id"]),
            str(snapshot_result["calculation_snapshot_hash"]),
        ),
    )

    finalize_request = s1ca._request(
        managed_workspace,
        envelope.COMMAND_FINALIZE,
        proposal_identity,
        idempotency_key=commands.canonical_finalize_key(proposal_id),
    )
    finalized = s1ca._run(managed_workspace, finalize_request)
    finalize_replay = s1ca._run(managed_workspace, finalize_request)
    assert finalized.exit_code == errors.EXIT_OK, finalized.response
    assert finalize_replay.exit_code == errors.EXIT_OK, finalize_replay.response
    assert finalize_replay.response["idempotent_replay"] is True
    assert finalized.response["result"]["path"] == "receipt"
    assert finalized.response["result"]["final_transaction_created"] is True
    assert "amount" not in finalized.response["result"]
    _assert_wrong_account_and_binding_replays_refused(
        managed_workspace,
        finalize_request,
        private_values=(
            proposal_id,
            receipt_public_id,
            str(snapshot_result["calculation_snapshot_hash"]),
        ),
    )
    assert dict(_domain_snapshot(managed_workspace))["transactions"] == 1
