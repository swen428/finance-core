"""Actual Ubuntu OCR proof through staging capture/propose/human completion.

Ordinary environments skip. The designated Bridge lane sets REQUIRED=1, making
platform, preparation and configuration absence failures, never green skips.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.receipt_ocr_evidence import (
    OcrResourceLimitExceededError,
    ReceiptOcrLimits,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.openclaw_staging_bridge import errors, ocr_boundary
from tests import test_openclaw_staging_bridge_guided_edit_v1 as human

FIXTURES = Path(__file__).parent / "fixtures" / "linux_receipt_ocr"


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    required = os.environ.get("FINANCE_LINUX_OCR_REQUIRED") == "1"
    config = os.environ.get("FINANCE_LINUX_OCR_CONFIG")
    if not required:
        pytest.skip("Actual Linux OCR is mandatory in the dedicated Ubuntu Bridge lane only.")
    assert sys.platform == "linux" and platform.machine() == "x86_64"
    release = platform.freedesktop_os_release()
    assert (release.get("ID"), release.get("VERSION_ID")) == ("ubuntu", "24.04")
    assert config and Path(config).is_file(), "Mandatory actual OCR preparation is missing"
    result = support.create_bridge_workspace(tmp_path)
    target = result.workspace_path / "runtime" / "ocr_engine.json"
    shutil.copyfile(config, target)
    target.chmod(0o600)
    return result


def capture(workspace: support.BridgeWorkspace, name: str) -> dict:
    content = (FIXTURES / name).read_bytes()
    support.write_handoff_file(workspace, name, content)
    outcome = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(
                workspace,
                handoff_filename=name,
                declared_mime_type="image/png" if name.endswith("png") else "image/jpeg",
            ),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    return outcome.response["result"]


def propose(workspace: support.BridgeWorkspace, intake: str) -> support.CliOutcome:
    return support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake,
            },
            idempotency_key=support.canonical_propose_key(intake),
        )
    )


def assert_evidence(
    workspace: support.BridgeWorkspace, name: str, *, expected_proposals: int = 1
) -> None:
    with support.open_database(workspace) as conn:
        extraction = conn.execute("SELECT * FROM receipt_ocr_extractions").fetchall()
        assert len(extraction) == 1
        assert extraction[0]["engine_name"] == "tesseract_tsv"
        assert (
            extraction[0]["engine_configuration_hash"]
            == ocr_boundary.resolve_workspace_ocr_engine(
                workspace.workspace_path
            ).identity.configuration_hash
        )
        image = conn.execute("SELECT * FROM attachments").fetchone()
        expected = (FIXTURES / name).read_bytes()
        assert image["file_hash"] == hashlib.sha256(expected).hexdigest()
        assert Path(image["file_path"]).read_bytes() == expected
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == expected_proposals
        )
        assert all(value == 0 for value in support.count_final_facts(conn).values())


@pytest.mark.parametrize("name", ["synthetic_mixed_receipt.jpg", "synthetic_mixed_receipt.png"])
def test_actual_mixed_receipt_capture_propose_replay(
    workspace: support.BridgeWorkspace, name: str
) -> None:
    captured = capture(workspace, name)
    outcome = propose(workspace, captured["intake_public_id"])
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    replay = propose(workspace, captured["intake_public_id"])
    assert replay.exit_code == errors.EXIT_OK and replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["proposal_public_id"]
        == outcome.response["result"]["proposal_public_id"]
    )
    with support.open_database(workspace) as conn:
        words = " ".join(
            row[0] for row in conn.execute("SELECT normalized_text FROM receipt_ocr_blocks")
        )
        compact = "".join(words.split())
        assert "TOTAL" in words.upper() and "TEST" in words.upper() and "12.34" in compact
        assert any(token in compact for token in ("合成", "收据", "测试", "商店")), words
        payload = json.loads(
            conn.execute("SELECT parsed_payload FROM parser_outputs").fetchone()[0]
        )
        assert payload["amount"] == "12.34", payload
    assert_evidence(workspace, name)


def test_actual_incomplete_receipt_human_completion_preserves_original_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    name = "synthetic_incomplete_receipt.png"
    captured = capture(workspace, name)
    outcome = propose(workspace, captured["intake_public_id"])
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    proposal = outcome.response["result"]["proposal_public_id"]
    review = support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": proposal},
        )
    )
    assert review.exit_code == errors.EXIT_OK, review.response
    card = review.response["result"]
    with support.open_database(workspace) as conn:
        original = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (proposal,)
        ).fetchone()[0]
        payload = json.loads(original)
        assert payload["amount"] is None and payload["status"] == "parsed_pending_confirmation", (
            payload
        )
        evidence_before = [
            tuple(row) for row in conn.execute("SELECT * FROM receipt_ocr_blocks ORDER BY id")
        ]
        words = " ".join(
            row[0] for row in conn.execute("SELECT normalized_text FROM receipt_ocr_blocks")
        )
        assert "MISSING" in words.upper() and "TEST" in words.upper(), words
        assert (
            conn.execute("SELECT extraction_status FROM receipt_ocr_extractions").fetchone()[0]
            == "succeeded"
        )
    batch = "e" * 32
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **human._context(workspace),
                "proposal_public_id": proposal,
                "reference_batch_id": batch,
                "token_ttl_seconds": 600,
                "expected_proposal_version": card["proposal_version"],
                "expected_content_hash": card["effective_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )
    assert issued.exit_code == errors.EXIT_OK, issued.response
    callback = "actual-linux-ocr-edit"
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **human._context(workspace),
                "short_reference": issued.response["result"]["actions"]["edit"]["reference"],
                "action": "edit",
                "callback_id": callback,
                "callback_message_id": 20,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback),
        )
    )
    assert redeemed.exit_code == errors.EXIT_OK, redeemed.response
    draft = redeemed.response["result"]["human_draft_card"]
    assert draft["completeness"] == "incomplete"
    completed = human._apply_whole_card(
        workspace, draft["card_generation_public_id"], merchant="Fictional Test Shop"
    )
    assert completed.exit_code == errors.EXIT_OK, completed.response
    result = completed.response["result"]
    assert result["completeness"] == "complete" and result["final_transaction_created"] is False
    assert result["human_reply_evidence_public_id"].startswith("d1evidence_")
    with support.open_database(workspace) as conn:
        assert (
            conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (proposal,)
            ).fetchone()[0]
            == original
        )
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM receipt_ocr_blocks ORDER BY id")
        ] == evidence_before
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0]
            == 1
        )
    assert_evidence(workspace, name, expected_proposals=2)


def test_actual_real_process_output_limit_refuses_without_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    engine = ocr_boundary.resolve_workspace_ocr_engine(workspace.workspace_path)
    capture(workspace, "synthetic_mixed_receipt.png")
    # This launches the actual version process and refuses its stdout at one byte;
    # it is real resource refusal, not a pre-launch expired-deadline claim.
    with support.open_database(workspace) as conn:
        attachment_id = conn.execute("SELECT id FROM attachments").fetchone()[0]
        with pytest.raises(OcrResourceLimitExceededError):
            extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_actual_limit",
                attachment_id=attachment_id,
                engine=engine,
                limits=ReceiptOcrLimits(max_stdout_bytes=1),
            )
        assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        assert all(value == 0 for value in support.count_final_facts(conn).values())


@pytest.mark.parametrize(
    "failure",
    [
        "missing_eng",
        "missing_chi_sim",
        "size",
        "hash",
        "symlink",
        "writable",
        "binary_hash",
        "version",
    ],
)
def test_actual_bad_resource_preserves_capture_and_refuses_ocr(
    workspace: support.BridgeWorkspace, tmp_path: Path, failure: str
) -> None:
    captured = capture(workspace, "synthetic_mixed_receipt.png")
    config_path = workspace.workspace_path / "runtime" / "ocr_engine.json"
    config = json.loads(config_path.read_text())
    models = tmp_path / "private-models"
    shutil.copytree(config["tessdata_directory"], models)
    models.chmod(0o700)
    config["tessdata_directory"] = str(models)
    path = models / "chi_sim.traineddata"
    if failure.startswith("missing_"):
        (models / f"{failure.removeprefix('missing_')}.traineddata").unlink()
    elif failure == "size":
        config["language_resources"][1]["size_bytes"] += 1
    elif failure == "hash":
        config["language_resources"][1]["sha256"] = "a" * 64
    elif failure == "symlink":
        path.unlink()
        path.symlink_to(models / "eng.traineddata")
    elif failure == "writable":
        path.chmod(0o622)
    elif failure == "binary_hash":
        config["binary_sha256"] = "a" * 64
    else:
        config["expected_version"] = "0.0.0"

    config_path.write_text(json.dumps(config))
    outcome = propose(workspace, captured["intake_public_id"])
    assert outcome.exit_code == (
        errors.EXIT_AUTHORITY_REFUSED if failure == "version" else errors.EXIT_VALIDATION_REFUSED
    )
    assert outcome.response["error"]["code"] == (
        errors.OCR_EXTRACTION_FAILED if failure == "version" else errors.OCR_ENGINE_UNAVAILABLE
    )
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records_v2").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        assert all(value == 0 for value in support.count_final_facts(conn).values())


def test_actual_ocr_process_deadline_fault_terminates_group_without_evidence(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject expiry after real OCR launch, without changing executable/output."""
    import time

    from finance_core.intake import receipt_ocr_evidence as ocr

    engine = ocr_boundary.resolve_workspace_ocr_engine(workspace.workspace_path)
    capture(workspace, "synthetic_mixed_receipt.png")
    actual_popen = ocr.subprocess.Popen
    actual_read = ocr._read_process_output
    ocr_pids: list[int] = []

    def observe_launch(arguments, **kwargs):
        child = actual_popen(arguments, **kwargs)
        if "tessedit_create_tsv=1" in arguments:
            ocr_pids.append(child.pid)
        return child

    def expire_after_actual_launch(process, *, limits, deadline):
        if process.pid in ocr_pids:
            deadline = time.monotonic() - 1
        return actual_read(process, limits=limits, deadline=deadline)

    monkeypatch.setattr(ocr.subprocess, "Popen", observe_launch)
    monkeypatch.setattr(ocr, "_read_process_output", expire_after_actual_launch)
    with support.open_database(workspace) as conn:
        attachment_id = conn.execute("SELECT id FROM attachments").fetchone()[0]
        with pytest.raises(ocr.OcrDeadlineExceededError):
            extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_actual_deadline_fault",
                attachment_id=attachment_id,
                engine=engine,
            )
        assert len(ocr_pids) == 1, "The real OCR process must actually launch"
        with pytest.raises(ProcessLookupError):
            os.kill(ocr_pids[0], 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(ocr_pids[0], 0)
        assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        assert all(value == 0 for value in support.count_final_facts(conn).values())
