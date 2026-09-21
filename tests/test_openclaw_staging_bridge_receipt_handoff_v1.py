"""S2 contract tests: receipt-image local handoff capture.

Covers handoff validation (size, symlink, signature, MIME, filename),
durable no-overwrite publication through the shared seam extracted from the
Telegram attachment acquisition saga, orphan reuse after a simulated crash
between publication and persistence, replay and conflict semantics, evidence
preservation, and retained handoff evidence after durable success.  All data is
temporary and synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake import attachment_publication as publication
from finance_core.openclaw_staging_bridge import errors as bridge_errors


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


class TestReceiptCaptureSuccess:
    def test_receipt_capture_uses_inherited_descriptor_without_reopening_path(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "descriptor.jpg", support.JPEG_BYTES)
        try:
            saved_fd3 = os.dup(3)
        except OSError:
            saved_fd3 = None
        source_fd = os.open(handoff_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.dup2(source_fd, 3, inheritable=True)
            if source_fd != 3:
                os.close(source_fd)
                source_fd = -1
            displaced = handoff_path.with_suffix(".displaced")
            handoff_path.rename(displaced)
            handoff_path.write_bytes(b"foreign path bytes")
            handoff_path.chmod(0o600)
            arguments = support.capture_receipt_arguments(
                workspace, handoff_filename="descriptor.jpg"
            )
            arguments["handoff_descriptor_fd"] = 3
            arguments["handoff_content_hash"] = hashlib.sha256(support.JPEG_BYTES).hexdigest()
            outcome = support.run_cli(
                support.make_request(
                    "capture",
                    arguments,
                    idempotency_key=support.canonical_capture_key(message_id=20),
                )
            )
        finally:
            if source_fd >= 0 and source_fd != 3:
                os.close(source_fd)
            if saved_fd3 is None:
                try:
                    os.close(3)
                except OSError:
                    pass
            else:
                os.dup2(saved_fd3, 3)
                os.close(saved_fd3)
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        assert (
            outcome.response["result"]["attachment_content_hash"]
            == hashlib.sha256(support.JPEG_BYTES).hexdigest()
        )

    def test_openclaw_normalized_receipt_message_date_is_preserved(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "normalized.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(
            workspace,
            handoff_filename="normalized.jpg",
            declared_mime_type="image/jpeg",
        )
        arguments.pop("telegram_update_id")
        arguments["telegram_message_date"] = 1_750_000_000
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        conn = support.open_database(workspace)
        try:
            row = conn.execute("SELECT source_payload FROM raw_intake_evidence").fetchone()
            assert row is not None
            source_payload = json.loads(row["source_payload"])
            assert source_payload["telegram_message_date"] == "1750000000"
            assert source_payload["source_received_at"] == "2025-06-15T15:06:40+00:00"
            assert "telegram_update_id" not in source_payload
        finally:
            conn.close()

    def test_openclaw_receipt_replay_preserves_first_message_date(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "normalized-replay.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(
            workspace, handoff_filename="normalized-replay.jpg"
        )
        arguments.pop("telegram_update_id")
        arguments["telegram_message_date"] = 1_750_000_000
        request = support.make_request(
            "capture",
            arguments,
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK, first.response

        changed = dict(request)
        changed["arguments"] = dict(request["arguments"])
        changed["arguments"]["telegram_message_date"] = 1_750_000_001
        replay = support.run_cli(changed)
        assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
        assert replay.response["idempotent_replay"] is True
        conn = support.open_database(workspace)
        try:
            row = conn.execute("SELECT source_payload FROM raw_intake_evidence").fetchone()
            assert row is not None
            assert json.loads(row["source_payload"])["telegram_message_date"] == "1750000000"
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
        finally:
            conn.close()

    def test_jpeg_capture_persists_intake_and_evidence(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "receipt_001.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(
            workspace,
            handoff_filename="receipt_001.jpg",
            declared_mime_type="image/jpeg",
            original_filename="receipt_001.jpg",
            caption="lunch receipt",
        )
        arguments.update(
            {
                "authenticated_actor_id": "111",
                "telegram_account_id": "finance-bot",
                "telegram_conversation_id": "111",
                "conversation_binding_id": "session-111",
            }
        )
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["capture_kind"] == "receipt_image"
        assert result["intake_public_id"].startswith("raw_intake_bridge_")
        assert result["attachment_evidence_public_id"].startswith("tgae_bridge_")
        assert result["attachment_content_hash"] == support.sha256_hex(support.JPEG_BYTES)
        assert result["mime_type"] == "image/jpeg"
        assert result["observed_file_size"] == len(support.JPEG_BYTES)
        assert result["final_transaction_created"] is False
        # Absolute attachment paths are never exposed in the envelope.
        assert str(workspace.workspace_path) not in str(result)

        # S5b0 keeps the private handoff as bounded replay/recovery evidence.
        assert handoff_path.read_bytes() == support.JPEG_BYTES
        assert stat.S_IMODE(os.lstat(handoff_path).st_mode) == 0o600

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            evidence = conn.execute(
                "SELECT content_hash, observed_file_size FROM telegram_attachment_source"
            ).fetchone()
            assert evidence["content_hash"] == support.sha256_hex(support.JPEG_BYTES)
            raw_input = conn.execute(
                "SELECT raw_input, source_type, source_channel FROM raw_intake_records"
            ).fetchone()
            assert raw_input["raw_input"] == "lunch receipt"
            assert raw_input["source_type"] == "telegram_image"
            assert raw_input["source_channel"] == "telegram"
            source_context = conn.execute(
                "SELECT telegram_account_id, telegram_conversation_id, "
                "conversation_binding_id, source_message_id "
                "FROM d2_telegram_source_contexts"
            ).fetchone()
            assert tuple(source_context) == ("finance-bot", "111", "session-111", "20")
        finally:
            conn.close()

        # Durable content-addressed bytes exist at mode 0400 inside the shard.
        shard = workspace.workspace_path / "attachments" / result["attachment_content_hash"][:2]
        durable = shard / f"{result['attachment_content_hash']}.jpg"
        assert durable.exists()
        assert stat.S_IMODE(os.lstat(durable).st_mode) == 0o400

    def test_png_capture_is_supported(self, workspace: support.BridgeWorkspace) -> None:
        support.write_handoff_file(workspace, "receipt_002.png", support.PNG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace,
                    handoff_filename="receipt_002.png",
                    declared_mime_type="image/png",
                ),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        assert outcome.response["result"]["mime_type"] == "image/png"

    def test_identical_replay_reuses_durable_bytes(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "receipt_003.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="receipt_003.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK

        # A later invocation reopens durable truth and retains the same handoff.
        second = support.run_cli(request)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True
        assert (
            second.response["result"]["attachment_content_hash"]
            == first.response["result"]["attachment_content_hash"]
        )
        assert handoff_path.read_bytes() == support.JPEG_BYTES

        # Re-publishing identical caller bytes also succeeds without per-file cleanup.
        replay_path = support.write_handoff_file(workspace, "receipt_003.jpg", support.JPEG_BYTES)
        third = support.run_cli(request)
        assert third.exit_code == bridge_errors.EXIT_OK
        assert third.response["idempotent_replay"] is True
        assert replay_path.read_bytes() == support.JPEG_BYTES
        assert stat.S_IMODE(os.lstat(replay_path).st_mode) == 0o600

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
        finally:
            conn.close()

    def test_fresh_process_restart_replays_and_retains_handoff(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "restart.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="restart.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK

        completed = subprocess.run(
            [sys.executable, "-m", "finance_core.openclaw_staging_bridge.cli"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        assert completed.returncode == bridge_errors.EXIT_OK, completed.stderr
        response = json.loads(completed.stdout)
        assert response["status"] == "ok"
        assert response["idempotent_replay"] is True
        assert handoff_path.read_bytes() == support.JPEG_BYTES
        assert stat.S_IMODE(os.lstat(handoff_path).st_mode) == 0o600


class TestReceiptCaptureRefusals:
    @pytest.mark.parametrize("field", ["caption", "original_filename"])
    def test_receipt_string_with_invalid_unicode_scalar_is_refused_before_persistence(
        self, workspace: support.BridgeWorkspace, field: str
    ) -> None:
        arguments = support.capture_receipt_arguments(
            workspace, handoff_filename="missing-invalid-caption.jpg"
        )
        arguments[field] = "taxi\ud800"
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
            )
        finally:
            conn.close()

    @pytest.mark.parametrize(
        ("field", "value", "error_code"),
        [
            ("original_filename", " receipt.jpg", bridge_errors.ARGUMENTS_REFUSED),
            ("original_filename", "receipt.jpg ", bridge_errors.ARGUMENTS_REFUSED),
            ("declared_mime_type", " image/jpeg", bridge_errors.ARGUMENTS_REFUSED),
            ("declared_mime_type", "image/jpeg ", bridge_errors.ARGUMENTS_REFUSED),
            ("declared_mime_type", "application/pdf", bridge_errors.HANDOFF_REFUSED),
            ("original_filename", "receipt.png", bridge_errors.HANDOFF_REFUSED),
        ],
    )
    def test_receipt_metadata_is_refused_before_raw_intake_or_durable_publication(
        self,
        workspace: support.BridgeWorkspace,
        field: str,
        value: str,
        error_code: str,
    ) -> None:
        handoff_path = support.write_handoff_file(
            workspace,
            "invalid-metadata.jpg",
            support.JPEG_BYTES,
        )
        arguments = support.capture_receipt_arguments(
            workspace,
            handoff_filename="invalid-metadata.jpg",
        )
        arguments[field] = value
        request = support.make_request(
            "capture",
            arguments,
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == error_code
        assert handoff_path.read_bytes() == support.JPEG_BYTES
        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
            )
        finally:
            conn.close()
        assert [
            path for path in (workspace.workspace_path / "attachments").rglob("*") if path.is_file()
        ] == []

        valid = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace,
                    handoff_filename="invalid-metadata.jpg",
                    original_filename="receipt.jpg",
                    declared_mime_type="image/jpeg",
                ),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert valid.exit_code == bridge_errors.EXIT_OK, valid.response

    @pytest.mark.parametrize(
        "message_date",
        [True, "1750000000", 1_750_000_000.0, -1, 1_262_303_999],
    )
    def test_openclaw_message_date_shape_is_refused_before_handoff_read(
        self, workspace: support.BridgeWorkspace, message_date: object
    ) -> None:
        arguments = support.capture_receipt_arguments(
            workspace, handoff_filename="missing-date-shape.jpg"
        )
        arguments.pop("telegram_update_id")
        arguments["telegram_message_date"] = message_date
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    @pytest.mark.parametrize("identity_mode", ["neither", "both"])
    def test_receipt_requires_exactly_one_transport_timestamp(
        self, workspace: support.BridgeWorkspace, identity_mode: str
    ) -> None:
        arguments = support.capture_receipt_arguments(
            workspace, handoff_filename="missing-identity.jpg"
        )
        if identity_mode == "neither":
            arguments.pop("telegram_update_id")
        else:
            arguments["telegram_message_date"] = 1_750_000_000
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_openclaw_message_date_out_of_range_is_refused_before_persistence(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "date-range.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(workspace, handoff_filename="date-range.jpg")
        arguments.pop("telegram_update_id")
        arguments["telegram_message_date"] = 10**30
        outcome = support.run_cli(
            support.make_request(
                "capture",
                arguments,
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
        assert handoff_path.read_bytes() == support.JPEG_BYTES
        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
            )
        finally:
            conn.close()

    def test_oversized_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        from finance_core.openclaw_staging_bridge.receipt_handoff import MAX_HANDOFF_BYTES

        oversized = support.JPEG_BYTES + b"x" * (MAX_HANDOFF_BYTES)
        support.write_handoff_file(workspace, "big.jpg", oversized)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="big.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_symlinked_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        real = workspace.workspace_path / "outside.jpg"
        real.write_bytes(support.JPEG_BYTES)
        handoff_dir = workspace.workspace_path / "handoff"
        handoff_dir.mkdir(mode=0o700, exist_ok=True)
        link = handoff_dir / "link.jpg"
        link.symlink_to(real)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="link.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_non_image_signature_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        support.write_handoff_file(workspace, "text.jpg", b"plain text pretending")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="text.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_pdf_signature_is_refused_for_receipts(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "doc.jpg", b"%PDF-1.7\nnot a receipt")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="doc.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_declared_mime_conflicting_with_signature_is_refused(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "conflict.jpg", support.JPEG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace,
                    handoff_filename="conflict.jpg",
                    declared_mime_type="image/png",
                ),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_unsupported_declared_mime_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        support.write_handoff_file(workspace, "app.jpg", support.JPEG_BYTES)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace,
                    handoff_filename="app.jpg",
                    declared_mime_type="application/pdf",
                ),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_missing_handoff_file_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="ghost.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_NOT_FOUND

    @pytest.mark.parametrize(
        "bad_name", ["../escape.jpg", ".hidden.jpg", "a/b.jpg", "name\x00.jpg", "x\ud800.jpg"]
    )
    def test_unsafe_handoff_filenames_are_refused(
        self, workspace: support.BridgeWorkspace, bad_name: str
    ) -> None:
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename=bad_name),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED

    def test_empty_handoff_file_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        support.write_handoff_file(workspace, "empty.jpg", b"")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="empty.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED


class TestReceiptCrashReplayAndConflict:
    def test_publication_failure_leaves_no_evidence_and_keeps_handoff(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "crash.jpg", support.JPEG_BYTES)

        def fail_publication(*args: object, **kwargs: object) -> None:
            raise publication.DurablePublicationError("injected crash before publication")

        monkeypatch.setattr(publication, "publish_no_overwrite", fail_publication)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="crash.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_INTERNAL
        # The handoff file remains the replay source; nothing was persisted.
        assert handoff_path.exists()
        # Partial publication must leave no private temp residue behind.
        residue = [
            path
            for path in (workspace.workspace_path / "attachments").rglob("*")
            if path.name.startswith(".")
        ]
        assert residue == []
        conn = support.open_database(workspace)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
            )
        finally:
            conn.close()

    def test_orphan_bytes_are_reused_after_publication_crash(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "orphan.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="orphan.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )

        # First attempt crashes exactly between durable publication and
        # database persistence, leaving a verified content-addressed orphan.
        real_publish = publication.publish_no_overwrite
        crashed = {"done": False}

        def crash_after_publication(root: object, **kwargs: object) -> tuple[str, bool]:
            result = real_publish(root, **kwargs)  # type: ignore[arg-type]
            if not crashed["done"]:
                crashed["done"] = True
                raise publication.DurablePublicationError("injected crash after publication")
            return result

        monkeypatch.setattr(publication, "publish_no_overwrite", crash_after_publication)
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_INTERNAL

        content_hash = support.sha256_hex(support.JPEG_BYTES)
        orphan = workspace.workspace_path / "attachments" / content_hash[:2] / f"{content_hash}.jpg"
        assert orphan.exists()

        # Replay reuses the orphan instead of re-acquiring and succeeds.
        monkeypatch.undo()
        second = support.run_cli(request)
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["result"]["durable_file_reused"] is True
        assert second.response["result"]["attachment_content_hash"] == content_hash
        assert handoff_path.read_bytes() == support.JPEG_BYTES

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
        finally:
            conn.close()

    def test_lost_response_after_evidence_commit_retains_handoff_for_retry(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from finance_core.openclaw_staging_bridge import receipt_handoff

        handoff_path = support.write_handoff_file(workspace, "lost.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="lost.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        real_persist = receipt_handoff.persist_attachment_evidence

        def lose_response_after_commit(*args: object, **kwargs: object) -> object:
            result = real_persist(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError(f"injected lost response after {result['public_id']}")

        monkeypatch.setattr(
            receipt_handoff, "persist_attachment_evidence", lose_response_after_commit
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_INTERNAL
        assert handoff_path.read_bytes() == support.JPEG_BYTES

        monkeypatch.undo()
        retry = support.run_cli(request)
        assert retry.exit_code == bridge_errors.EXIT_OK
        assert retry.response["idempotent_replay"] is True
        assert handoff_path.read_bytes() == support.JPEG_BYTES

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
        finally:
            conn.close()

    def test_final_handoff_unlink_is_never_attempted(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_path = support.write_handoff_file(workspace, "race.jpg", support.JPEG_BYTES)
        attempted: list[Path] = []
        real_unlink = Path.unlink

        def refuse_final_handoff_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path == handoff_path:
                attempted.append(path)
                raise AssertionError("per-file final handoff unlink is forbidden")
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse_final_handoff_unlink)
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="race.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        assert attempted == []
        assert handoff_path.read_bytes() == support.JPEG_BYTES

    def test_replay_with_different_content_conflicts(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "conflict2.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="conflict2.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK

        different = support.JPEG_BYTES + b"different-content"
        support.write_handoff_file(workspace, "conflict2.jpg", different)
        second = support.run_cli(request)
        assert second.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert second.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_same_telegram_message_text_capture_conflicts_with_receipt(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        text_outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(
                    workspace,
                    support.telegram_text_update("caption text", message_id=30),
                ),
                idempotency_key=support.canonical_capture_key(message_id=30),
            )
        )
        assert text_outcome.exit_code == bridge_errors.EXIT_OK

        support.write_handoff_file(workspace, "conflict3.jpg", support.JPEG_BYTES)
        receipt_outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(
                    workspace, handoff_filename="conflict3.jpg", message_id=30
                ),
                idempotency_key=support.canonical_capture_key(message_id=30),
            )
        )
        assert receipt_outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert receipt_outcome.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT


class TestCrashWindowBindingAndRaceClassification:
    """Reviewer-bound regressions for the publish/persist crash window.

    The intake row binds the handoff content hash into its fingerprint before
    publication, so a replay carrying different content inside the window
    between intake persistence and evidence persistence fails closed with
    IDEMPOTENCY_CONFLICT, and concurrent first-capture races classify through
    the persisted winner instead of surfacing a retryable internal error.
    """

    def test_crash_window_replay_with_different_content_fails_closed(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from finance_core.openclaw_staging_bridge import receipt_handoff

        support.write_handoff_file(workspace, "window.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="window.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )

        def fail_persistence(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected crash before evidence persistence")

        monkeypatch.setattr(receipt_handoff, "persist_attachment_evidence", fail_persistence)
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_INTERNAL
        monkeypatch.undo()

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
            )
        finally:
            conn.close()

        # Different content under the same identity must fail closed.
        support.write_handoff_file(workspace, "window.jpg", support.PNG_BYTES)
        conflict = support.run_cli(request)
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

        # The original content still recovers through orphan reuse.
        support.write_handoff_file(workspace, "window.jpg", support.JPEG_BYTES)
        recovery = support.run_cli(request)
        assert recovery.exit_code == bridge_errors.EXIT_OK
        assert recovery.response["result"]["attachment_content_hash"] == support.sha256_hex(
            support.JPEG_BYTES
        )

    def test_first_capture_race_is_classified_not_internal(
        self,
        workspace: support.BridgeWorkspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from finance_core.openclaw_staging_bridge import commands as bridge_commands

        support.write_handoff_file(workspace, "race.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(workspace, handoff_filename="race.jpg")
        first = support.run_cli(
            support.make_request(
                "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=20)
            )
        )
        assert first.exit_code == bridge_errors.EXIT_OK

        # Force the pre-insert lookup to miss so the insert collides with the
        # persisted winner exactly like a concurrent first capture.
        real_lookup = bridge_commands.get_raw_intake_record_by_idempotency_key
        state = {"missed": False}

        def lookup_missing_first(conn: object, key: str) -> object:
            if not state["missed"]:
                state["missed"] = True
                return None
            return real_lookup(conn, key)  # type: ignore[arg-type]

        monkeypatch.setattr(
            bridge_commands, "get_raw_intake_record_by_idempotency_key", lookup_missing_first
        )

        support.write_handoff_file(workspace, "race.jpg", support.PNG_BYTES)
        conflict = support.run_cli(
            support.make_request(
                "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=20)
            )
        )
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
        assert conflict.response["error"]["retryable"] is False

        # The identical-content race resolves as an idempotent replay.
        support.write_handoff_file(workspace, "race.jpg", support.JPEG_BYTES)
        state["missed"] = False
        replay = support.run_cli(
            support.make_request(
                "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=20)
            )
        )
        assert replay.exit_code == bridge_errors.EXIT_OK

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
        finally:
            conn.close()


class TestReplayHandoffRefusals:
    """A replay whose handoff reappears unsafe must fail closed, not recover.

    Only a genuinely missing handoff may recover from durable evidence.
    Empty, oversized, symlinked, non-regular, or unreadable handoff files
    refuse stably, keep the offending file in place, and change neither the
    database nor the durable attachment store.
    """

    def _capture(self, workspace: support.BridgeWorkspace) -> dict:
        support.write_handoff_file(workspace, "replay.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="replay.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
        return request

    def _durable_snapshot(self, workspace: support.BridgeWorkspace) -> tuple[dict, list, str]:
        conn = support.open_database(workspace)
        try:
            counts = {
                "raw_intake_records": conn.execute(
                    "SELECT COUNT(*) FROM raw_intake_records"
                ).fetchone()[0],
                "telegram_attachment_source": conn.execute(
                    "SELECT COUNT(*) FROM telegram_attachment_source"
                ).fetchone()[0],
            }
            evidence_hash = str(
                conn.execute("SELECT content_hash FROM telegram_attachment_source").fetchone()[
                    "content_hash"
                ]
            )
        finally:
            conn.close()
        durable_files = sorted(
            (
                str(path.relative_to(workspace.workspace_path)),
                support.sha256_hex(path.read_bytes()),
            )
            for path in (workspace.workspace_path / "attachments").rglob("*")
            if path.is_file()
        )
        return counts, durable_files, evidence_hash

    def _assert_refused_without_side_effects(
        self,
        workspace: support.BridgeWorkspace,
        request: dict,
        handoff_path: Path,
        before: tuple[dict, list, str],
    ) -> None:
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED, outcome.response
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED
        assert outcome.response["error"]["retryable"] is False
        # The unsafe handoff is left in place for inspection, never deleted.
        assert handoff_path.exists() or handoff_path.is_symlink()
        counts, durable_files, evidence_hash = before
        after_counts, after_files, after_hash = self._durable_snapshot(workspace)
        assert after_counts == counts
        assert after_files == durable_files
        assert after_hash == evidence_hash

    def test_replay_without_handoff_recovers_from_durable_evidence(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        request = self._capture(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        # Simulate a legacy/external missing-handoff state only to preserve the
        # existing replay compatibility; the capture boundary never unlinks it.
        handoff_path.unlink()
        assert not handoff_path.exists()
        replay = support.run_cli(request)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True

    def test_replay_without_optional_filename_uses_persisted_evidence(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        support.write_handoff_file(workspace, "filename-replay.jpg", support.JPEG_BYTES)
        request = support.make_request(
            "capture",
            support.capture_receipt_arguments(
                workspace,
                handoff_filename="filename-replay.jpg",
                original_filename="receipt.jpg",
            ),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
        first = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK, first.response
        request["arguments"].pop("original_filename")
        replay = support.run_cli(request)
        assert replay.exit_code == bridge_errors.EXIT_OK
        assert replay.response["idempotent_replay"] is True

        conn = support.open_database(workspace)
        try:
            row = conn.execute(
                "SELECT original_filename FROM telegram_attachment_source"
            ).fetchone()
            assert row is not None
            assert row["original_filename"] == "receipt.jpg"
        finally:
            conn.close()

        request["arguments"]["original_filename"] = "different.jpg"
        conflict = support.run_cli(request)
        assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_empty_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = self._capture(workspace)
        before = self._durable_snapshot(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        handoff_path.write_bytes(b"")
        self._assert_refused_without_side_effects(workspace, request, handoff_path, before)

    def test_oversized_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        from finance_core.openclaw_staging_bridge.receipt_handoff import MAX_HANDOFF_BYTES

        request = self._capture(workspace)
        before = self._durable_snapshot(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        with open(handoff_path, "wb") as handle:
            handle.truncate(MAX_HANDOFF_BYTES + 1)
        self._assert_refused_without_side_effects(workspace, request, handoff_path, before)

    def test_symlinked_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = self._capture(workspace)
        before = self._durable_snapshot(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        target = workspace.workspace_path / "handoff" / "real-target.jpg"
        target.write_bytes(support.JPEG_BYTES)
        handoff_path.unlink()
        os.symlink(str(target), str(handoff_path))
        self._assert_refused_without_side_effects(workspace, request, handoff_path, before)
        target.unlink()

    def test_non_regular_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = self._capture(workspace)
        before = self._durable_snapshot(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        handoff_path.unlink()
        handoff_path.mkdir()
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.HANDOFF_REFUSED
        assert handoff_path.is_dir()
        counts, durable_files, evidence_hash = before
        after_counts, after_files, after_hash = self._durable_snapshot(workspace)
        assert after_counts == counts
        assert after_files == durable_files
        assert after_hash == evidence_hash
        handoff_path.rmdir()

    def test_unreadable_handoff_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        if os.geteuid() == 0:
            pytest.skip("Root bypasses file permission bits.")
        request = self._capture(workspace)
        before = self._durable_snapshot(workspace)
        handoff_path = workspace.workspace_path / "handoff" / "replay.jpg"
        handoff_path.write_bytes(support.JPEG_BYTES)
        handoff_path.chmod(0o000)
        try:
            self._assert_refused_without_side_effects(workspace, request, handoff_path, before)
        finally:
            handoff_path.chmod(0o600)
