"""Actual Ubuntu OCR proof through staging capture/propose/human completion.

Ordinary environments skip. The designated Bridge lane sets REQUIRED=1, making
platform, preparation and configuration absence failures, never green skips.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake import receipt_ocr_proposal
from finance_core.intake.receipt_ocr_evidence import (
    OcrResourceLimitExceededError,
    ReceiptOcrLimits,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.openclaw_staging_bridge import errors, ocr_boundary
from finance_core.parser_proposals import receipt_total_parser
from tests import test_openclaw_staging_bridge_guided_edit_v1 as human

FIXTURES = Path(__file__).parent / "fixtures" / "linux_receipt_ocr"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_DIAGNOSTIC_BLOCKS = 256
MAX_DIAGNOSTIC_TEXT_BYTES = 1024
MAX_DIAGNOSTIC_JSON_BYTES = 512 * 1024
DIAGNOSTIC_FILENAMES = {
    "synthetic_mixed_receipt.jpg": "mixed-jpeg.json",
    "synthetic_mixed_receipt.png": "mixed-png.json",
}
DIAGNOSTIC_TOP_LEVEL_KEYS = {
    "schema",
    "fixture",
    "source",
    "run",
    "engine",
    "extraction",
    "stored_ocr_hierarchy",
    "loader_view",
    "parser_grouped_lines",
    "proposal",
    "counts",
}
DIAGNOSTIC_FORBIDDEN_KEYS = {
    "path",
    "file_path",
    "database_path",
    "workspace_path",
    "helper_path",
    "tessdata_directory",
    "environment",
    "environment_variables",
    "env",
    "credentials",
    "token",
    "secret",
}


def _bounded_run_identity() -> dict[str, str | None]:
    fields = {
        "event_name": ("GITHUB_EVENT_NAME", r"[a-z_]{1,32}"),
        "run_id": ("GITHUB_RUN_ID", r"[0-9]{1,20}"),
        "run_attempt": ("GITHUB_RUN_ATTEMPT", r"[0-9]{1,4}"),
        "sha": ("GITHUB_SHA", r"[0-9a-f]{40}"),
        "ref": ("GITHUB_REF", r"refs/(?:heads|tags)/[A-Za-z0-9._/-]{1,200}"),
    }
    result: dict[str, str | None] = {}
    for key, (env_name, pattern) in fields.items():
        value = os.environ.get(env_name)
        if value is not None and re.fullmatch(pattern, value) is None:
            raise AssertionError("Linux OCR diagnostic run identity is malformed")
        result[key] = value
    return result


def _assert_diagnostic_fields_are_bounded(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in DIAGNOSTIC_FORBIDDEN_KEYS:
                raise AssertionError("Linux OCR diagnostic contains a prohibited field")
            _assert_diagnostic_fields_are_bounded(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _assert_diagnostic_fields_are_bounded(item)
    elif isinstance(value, str) and len(value.encode("utf-8")) > MAX_DIAGNOSTIC_TEXT_BYTES:
        raise AssertionError("Linux OCR diagnostic text exceeds its per-field bound")


def _write_diagnostic_artifact(fixture_name: str, payload: dict[str, Any]) -> None:
    output_value = os.environ.get("FINANCE_LINUX_OCR_DIAGNOSTIC_DIR")
    if output_value is None:
        return
    if not output_value:
        raise AssertionError("Linux OCR diagnostic output directory is empty")
    artifact_name = DIAGNOSTIC_FILENAMES.get(fixture_name)
    if artifact_name is None:
        raise AssertionError("Linux OCR diagnostic fixture is not allowlisted")
    if set(payload) != DIAGNOSTIC_TOP_LEVEL_KEYS:
        raise AssertionError("Linux OCR diagnostic fields do not match the bounded schema")
    _assert_diagnostic_fields_are_bounded(payload)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_DIAGNOSTIC_JSON_BYTES:
        raise AssertionError("Linux OCR diagnostic exceeds its file-size bound")

    configured_dir = Path(output_value)
    if not configured_dir.is_absolute() or configured_dir.is_symlink():
        raise AssertionError("Linux OCR diagnostic output must be an absolute owned directory")
    output_dir = configured_dir.resolve()
    if output_dir.is_relative_to(REPOSITORY_ROOT) or REPOSITORY_ROOT.is_relative_to(output_dir):
        raise AssertionError("Linux OCR diagnostic output must be outside the checkout")
    try:
        output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(output_dir.stat().st_mode) or output_dir.stat().st_mode & 0o077:
            raise AssertionError("Linux OCR diagnostic directory permissions are too broad")
        destination = output_dir / artifact_name
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
    except OSError:
        raise AssertionError("Linux OCR diagnostic artifact could not be written safely") from None


def _write_actual_ocr_diagnostic(
    conn,
    *,
    fixture_name: str,
    intake_public_id: str,
    proposal_public_id: str,
) -> None:
    if os.environ.get("FINANCE_LINUX_OCR_DIAGNOSTIC_DIR") is None:
        return
    if fixture_name not in DIAGNOSTIC_FILENAMES:
        raise AssertionError("Linux OCR diagnostic fixture is not allowlisted")

    fixture_bytes = (FIXTURES / fixture_name).read_bytes()
    capture = conn.execute(
        "SELECT public_id, attachment_id FROM raw_intake_records WHERE public_id = ?",
        (intake_public_id,),
    ).fetchone()
    assert capture is not None
    attachment = conn.execute(
        "SELECT public_id, file_hash, mime_type FROM attachments WHERE id = ?",
        (capture["attachment_id"],),
    ).fetchone()
    assert attachment is not None
    extraction_row = conn.execute(
        """
        SELECT id, public_id, engine_name, engine_version, engine_binary_sha256,
               engine_configuration_hash, extraction_status, block_count,
               normalized_result_hash
        FROM receipt_ocr_extractions WHERE attachment_id = ?
        ORDER BY id
        """,
        (capture["attachment_id"],),
    ).fetchone()
    assert extraction_row is not None
    extraction = receipt_ocr_proposal._load_extraction(conn, extraction_row["public_id"])
    if extraction.block_count > MAX_DIAGNOSTIC_BLOCKS:
        raise AssertionError("Linux OCR diagnostic block count exceeds its read bound")
    stored_rows = conn.execute(
        """
        SELECT sequence_index, page_index, engine_block_index,
               engine_paragraph_index, engine_line_index, engine_word_index,
               normalized_text, coordinate_left, coordinate_top,
               coordinate_width, coordinate_height, page_width, page_height,
               confidence_scaled
        FROM receipt_ocr_blocks
        WHERE extraction_id = ?
        ORDER BY sequence_index
        LIMIT ?
        """,
        (extraction.id, MAX_DIAGNOSTIC_BLOCKS + 1),
    ).fetchall()
    if len(stored_rows) > MAX_DIAGNOSTIC_BLOCKS:
        raise AssertionError("Linux OCR diagnostic block count exceeds its read bound")

    loader_blocks = receipt_ocr_proposal._load_blocks(conn, extraction)
    grouped_lines = receipt_total_parser._build_lines(loader_blocks)
    proposal_row = conn.execute(
        """
        SELECT parser_outputs.public_id, parser_outputs.parsed_payload
        FROM parser_outputs
        JOIN receipt_ocr_proposal_links
          ON receipt_ocr_proposal_links.parser_output_id = parser_outputs.id
        WHERE receipt_ocr_proposal_links.extraction_id = ?
          AND parser_outputs.public_id = ?
        """,
        (extraction.id, proposal_public_id),
    ).fetchone()
    assert proposal_row is not None
    parsed_payload = json.loads(proposal_row["parsed_payload"])
    extraction_counts = conn.execute(
        "SELECT COUNT(*) FROM receipt_ocr_extractions WHERE attachment_id = ?",
        (capture["attachment_id"],),
    ).fetchone()[0]
    proposal_counts = conn.execute(
        """
        SELECT COUNT(*)
        FROM receipt_ocr_proposal_links
        WHERE extraction_id = ?
        """,
        (extraction.id,),
    ).fetchone()[0]
    parser_output_counts = conn.execute(
        """
        SELECT COUNT(*)
        FROM parser_outputs
        JOIN receipt_ocr_proposal_links
          ON receipt_ocr_proposal_links.parser_output_id = parser_outputs.id
        WHERE receipt_ocr_proposal_links.extraction_id = ?
        """,
        (extraction.id,),
    ).fetchone()[0]
    final_counts = support.count_final_facts(conn)

    stored_hierarchy = []
    for row in stored_rows:
        text = row["normalized_text"]
        if len(text.encode("utf-8")) > MAX_DIAGNOSTIC_TEXT_BYTES:
            raise AssertionError("Linux OCR stored text exceeds its read bound")
        stored_hierarchy.append(
            {
                "sequence_index": row["sequence_index"],
                "page_index": row["page_index"],
                "engine_block_index": row["engine_block_index"],
                "engine_paragraph_index": row["engine_paragraph_index"],
                "engine_line_index": row["engine_line_index"],
                "engine_word_index": row["engine_word_index"],
                "normalized_text": text,
                "coordinate_left": row["coordinate_left"],
                "coordinate_top": row["coordinate_top"],
                "coordinate_width": row["coordinate_width"],
                "coordinate_height": row["coordinate_height"],
                "page_width": row["page_width"],
                "page_height": row["page_height"],
                "confidence_scaled": row["confidence_scaled"],
            }
        )

    payload: dict[str, Any] = {
        "schema": "finance-linux-ocr-synthetic-diagnostic-v1",
        "fixture": {
            "name": fixture_name,
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "size_bytes": len(fixture_bytes),
        },
        "source": {
            "intake_public_id": capture["public_id"],
            "attachment_public_id": attachment["public_id"],
            "sha256": attachment["file_hash"],
            "mime_type": attachment["mime_type"],
            "matches_fixture": attachment["file_hash"] == hashlib.sha256(fixture_bytes).hexdigest(),
        },
        "run": _bounded_run_identity(),
        "engine": {
            "name": extraction_row["engine_name"],
            "version": extraction_row["engine_version"],
            "binary_sha256": extraction_row["engine_binary_sha256"],
            "configuration_hash": extraction_row["engine_configuration_hash"],
        },
        "extraction": {
            "public_id": extraction_row["public_id"],
            "status": extraction_row["extraction_status"],
            "block_count": extraction_row["block_count"],
            "normalized_result_hash": extraction_row["normalized_result_hash"],
        },
        "stored_ocr_hierarchy": stored_hierarchy,
        "loader_view": [
            {
                "sequence_index": block.sequence_index,
                "page_index": block.page_index,
                "engine_block_index": block.engine_block_index,
                "engine_paragraph_index": block.engine_paragraph_index,
                "engine_line_index": block.engine_line_index,
                "text": block.text,
                "left": block.left,
                "top": block.top,
                "height": block.height,
                "confidence_scaled": block.confidence_scaled,
            }
            for block in loader_blocks
        ],
        "parser_grouped_lines": [
            {
                "page_index": line.page_index,
                "text": line.text,
                "contributors": list(line.block_sequence_indexes),
                "is_total": receipt_total_parser._is_total_line(line.upper),
                "extract_line_total": (
                    list(candidate)
                    if (candidate := receipt_total_parser._extract_line_total(line.upper))
                    is not None
                    else None
                ),
            }
            for line in grouped_lines
        ],
        "proposal": {
            "public_id": proposal_row["public_id"],
            "amount": parsed_payload["amount"],
            "currency": parsed_payload["currency"],
            "ambiguity_flags": parsed_payload["ambiguity_flags"],
        },
        "counts": {
            "capture": {
                "raw_intake_records": conn.execute(
                    "SELECT COUNT(*) FROM raw_intake_records WHERE public_id = ?",
                    (intake_public_id,),
                ).fetchone()[0],
                "attachments": conn.execute(
                    "SELECT COUNT(*) FROM attachments WHERE id = ?",
                    (capture["attachment_id"],),
                ).fetchone()[0],
            },
            "extractions": extraction_counts,
            "proposals": proposal_counts,
            "parser_outputs": parser_output_counts,
            "final_facts": final_counts,
        },
    }
    _write_diagnostic_artifact(fixture_name, payload)


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
        payload = json.loads(
            conn.execute("SELECT parsed_payload FROM parser_outputs").fetchone()[0]
        )
        _write_actual_ocr_diagnostic(
            conn,
            fixture_name=name,
            intake_public_id=captured["intake_public_id"],
            proposal_public_id=outcome.response["result"]["proposal_public_id"],
        )
        assert "TOTAL" in words.upper() and "TEST" in words.upper() and "12.34" in compact
        assert any(token in compact for token in ("合成", "收据", "测试", "商店")), words
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
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM raw_intake_records WHERE public_id = ?",
                (captured["intake_public_id"],),
            ).fetchone()[0]
            == 1
        )
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
