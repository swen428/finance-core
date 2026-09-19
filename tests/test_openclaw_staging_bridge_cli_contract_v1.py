"""S1 contract tests: bounded JSON CLI foundation of the staging bridge.

Covers the envelope validation matrix, byte/depth limits, exit codes,
health/get_status/capture(text), staging-only refusals, deterministic
operation identities, idempotent replay and conflicts, concurrency, and the
no-network/no-credential execution guarantees.  All databases and identities
are temporary and synthetic.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import commands as bridge_commands
from finance_core.openclaw_staging_bridge import envelope as bridge_envelope
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import workspace_access


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


# ---------------------------------------------------------------------------
# Envelope validation and limit matrix
# ---------------------------------------------------------------------------


class TestEnvelopeMatrix:
    def test_malformed_json_is_refused_with_exit_3(self) -> None:
        outcome = support.run_cli({}, raw_override=b"{not-json")
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["status"] == "error"
        assert outcome.response["error"]["code"] == bridge_errors.MALFORMED_ENVELOPE
        assert outcome.response["error"]["retryable"] is False

    def test_non_utf8_bytes_are_refused(self) -> None:
        outcome = support.run_cli({}, raw_override=b"\xff\xfe\x00")
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_non_object_root_is_refused(self) -> None:
        outcome = support.run_cli({}, raw_override=b"[1, 2, 3]")
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_unknown_top_level_field_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = support.make_request(
            "health",
            support.health_arguments(workspace),
            extra_fields={"unexpected": True},
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["error"]["code"] == bridge_errors.MALFORMED_ENVELOPE

    def test_missing_required_field_is_refused(self) -> None:
        request = support.make_request("health", {"workspace_path": "/tmp/does-not-matter"})
        del request["arguments"]
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_wrong_envelope_version_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = support.make_request(
            "health", support.health_arguments(workspace), envelope_version="v2"
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_malformed_request_id_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        request = support.make_request(
            "health", support.health_arguments(workspace), request_id="req_UPPER"
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_unknown_command_is_refused_with_exit_4(self) -> None:
        request = support.make_request("settle", {}, idempotency_key="key")
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_UNKNOWN_COMMAND
        assert outcome.response["error"]["code"] == bridge_errors.UNKNOWN_COMMAND

    @pytest.mark.parametrize(
        "command",
        (
            "prepare_ai_fallback",
            "claim_ai_fallback_invocation",
            "record_ai_fallback_result",
        ),
    )
    def test_s5e_b_commands_are_registered_and_validate_arguments(self, command: str) -> None:
        assert command in bridge_envelope.ALLOWED_COMMANDS
        request = support.make_request(command, {}, idempotency_key="key")
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    @pytest.mark.parametrize(
        ("command", "mutating"),
        (
            ("apply_human_draft_card", True),
            ("get_human_draft_card", False),
            ("begin_human_draft_card_delivery", True),
            ("record_human_draft_card_delivery_outcome", True),
            ("reissue_human_draft_card", True),
        ),
    )
    def test_d1_bridge_commands_are_registered_and_fail_closed_on_missing_arguments(
        self, command: str, mutating: bool
    ) -> None:
        request = support.make_request(
            command,
            {},
            **({"idempotency_key": "test-key"} if mutating else {}),
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_standalone_human_draft_begin_is_not_registered(self) -> None:
        outcome = support.run_cli(
            support.make_request("begin_human_draft", {}, idempotency_key="test-key")
        )
        assert outcome.exit_code == bridge_errors.EXIT_UNKNOWN_COMMAND
        assert outcome.response["error"]["code"] == bridge_errors.UNKNOWN_COMMAND

    def test_mutating_command_requires_idempotency_key(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("lunch 12.50")
        request = support.make_request("capture", support.capture_text_arguments(workspace, update))
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["error"]["code"] == bridge_errors.MISSING_IDEMPOTENCY_KEY

    @pytest.mark.parametrize("bad_key", ["", "   ", "x" * 201, "bad\nkey", "bad\x7fkey"])
    def test_invalid_idempotency_key_is_refused(
        self, workspace: support.BridgeWorkspace, bad_key: str
    ) -> None:
        update = support.telegram_text_update("lunch 12.50")
        request = support.make_request(
            "capture",
            support.capture_text_arguments(workspace, update),
            idempotency_key=bad_key,
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_non_object_arguments_are_refused(self, workspace: support.BridgeWorkspace) -> None:
        raw = json.dumps(
            {
                "envelope_version": "v1",
                "command": "health",
                "request_id": support.new_request_id(),
                "arguments": [],
            }
        ).encode("utf-8")
        outcome = support.run_cli({}, raw_override=raw)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE

    def test_oversized_request_is_refused_with_exit_3(self) -> None:
        outcome = support.run_cli({}, raw_override=support.max_request_envelope("health"))
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["error"]["code"] == bridge_errors.OVERSIZED_ENVELOPE

    def test_excessive_nesting_depth_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        deep: dict[str, object] = {}
        node = deep
        for index in range(bridge_envelope.MAX_ENVELOPE_DEPTH + 2):
            child: dict[str, object] = {}
            node[f"level{index}"] = child
            node = child
        request = support.make_request(
            "health",
            {"workspace_path": str(workspace.workspace_path), "nested": deep},
        )
        outcome = support.run_cli(request)
        assert outcome.exit_code == bridge_errors.EXIT_MALFORMED_ENVELOPE
        assert outcome.response["error"]["code"] == bridge_errors.ENVELOPE_TOO_DEEP

    def test_exactly_one_stdout_line_is_emitted(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace))
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        assert outcome.response["envelope_version"] == "v1"
        assert outcome.response["status"] == "ok"


# ---------------------------------------------------------------------------
# health and workspace/staging refusals
# ---------------------------------------------------------------------------


class TestHealthAndStagingGuard:
    def test_health_reports_verified_workspace(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace))
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["workspace_verified"] is True
        assert result["database_verified"] is True
        assert result["migration_ledger_count"] == len(support.TEMP_DB_MIGRATION_PATHS)
        expected_latest_migration = support.TEMP_DB_MIGRATION_PATHS[-1].name.partition("_")[0]
        assert result["latest_migration"] == expected_latest_migration
        assert result["callback_key_status"] == "present"
        assert outcome.response["idempotent_replay"] is False

    def test_missing_workspace_is_usage_error(self, tmp_path: Path) -> None:
        outcome = support.run_cli(
            support.make_request("health", {"workspace_path": str(tmp_path / "missing_workspace")})
        )
        assert outcome.exit_code == bridge_errors.EXIT_USAGE
        assert outcome.response["error"]["code"] == bridge_errors.WORKSPACE_MISSING
        assert outcome.response["error"]["retryable"] is True

    def test_workspace_inside_repository_is_refused(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        outcome = support.run_cli(
            support.make_request("health", {"workspace_path": str(repo_root / "database")})
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.WORKSPACE_REFUSED

    def test_relative_workspace_path_is_refused(self) -> None:
        outcome = support.run_cli(
            support.make_request("health", {"workspace_path": "relative/workspace"})
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED

    def test_symlinked_workspace_is_refused(
        self, workspace: support.BridgeWorkspace, tmp_path: Path
    ) -> None:
        link = tmp_path / "workspace_link"
        link.symlink_to(workspace.workspace_path)
        outcome = support.run_cli(support.make_request("health", {"workspace_path": str(link)}))
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.WORKSPACE_REFUSED

    def test_unsafe_database_directory_is_refused_before_database_open(
        self,
        workspace: support.BridgeWorkspace,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_dir = workspace.workspace_path / "database"
        external_database_dir = tmp_path / "external-database"
        database_dir.rename(external_database_dir)
        database_dir.symlink_to(external_database_dir, target_is_directory=True)
        opened = False

        def fail_database_open(_workspace: Path) -> sqlite3.Connection:
            nonlocal opened
            opened = True
            raise AssertionError("unsafe workspace must be refused before database open")

        monkeypatch.setattr(workspace_access, "open_workspace_database", fail_database_open)
        with pytest.raises(bridge_errors.BridgeError) as error:
            bridge_commands._open_context(
                {"workspace_path": str(workspace.workspace_path)},
                bridge_commands.Deadline(),
            )
        assert error.value.code == bridge_errors.WORKSPACE_REFUSED
        assert opened is False

    def test_unauthorized_database_is_refused_with_exit_6(
        self, workspace: support.BridgeWorkspace, tmp_path: Path
    ) -> None:
        # Replace the authorised staging database with an arbitrary SQLite file.
        rogue = workspace.workspace_path / "database" / "staging.sqlite"
        rogue.unlink()
        conn = sqlite3.connect(str(rogue))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace))
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STAGING_REFUSED
        assert outcome.response["error"]["retryable"] is True

    def test_copied_authorised_database_is_refused(
        self, workspace: support.BridgeWorkspace, tmp_path: Path
    ) -> None:
        copied_root = tmp_path / "copied_workspace"
        (copied_root / "database").mkdir(parents=True, mode=0o700)
        for directory in ("attachments", "runtime", "evidence"):
            (copied_root / directory).mkdir(mode=0o700)
        copied_db = copied_root / "database" / "staging.sqlite"
        copied_db.write_bytes(workspace.database_path.read_bytes())
        outcome = support.run_cli(
            support.make_request("health", {"workspace_path": str(copied_root)})
        )
        assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.STAGING_REFUSED


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_requires_exactly_one_identity(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request("get_status", {"workspace_path": str(workspace.workspace_path)})
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED

        outcome = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": "raw_intake_x",
                    "proposal_public_id": "parser_output_x",
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED

    def test_unknown_intake_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": "raw_intake_missing",
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.INTAKE_NOT_FOUND

    def test_status_after_text_capture(self, workspace: support.BridgeWorkspace) -> None:
        update = support.telegram_text_update("dinner 42.00")
        capture = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(workspace, update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert capture.exit_code == bridge_errors.EXIT_OK
        intake_public_id = capture.response["result"]["intake_public_id"]

        outcome = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": intake_public_id,
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["identity_kind"] == "intake"
        assert result["intake_status"] == "parsed_pending_confirmation"
        assert result["parse_status"] == "parsed_pending_confirmation"
        assert result["final_transaction_created"] is False

        proposal_status = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": result["proposal_public_id"],
                },
            )
        )
        assert proposal_status.exit_code == bridge_errors.EXIT_OK
        proposal_result = proposal_status.response["result"]
        assert proposal_result["identity_kind"] == "proposal"
        assert proposal_result["proposal_version"] == 0
        assert len(proposal_result["effective_content_hash"]) == 64
        assert proposal_result["conversion_status"] == "not_converted"


# ---------------------------------------------------------------------------
# capture (text)
# ---------------------------------------------------------------------------


class TestCaptureText:
    def test_capture_persists_raw_intake_and_proposal(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("coffee 6.40")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(workspace, update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK
        result = outcome.response["result"]
        assert result["capture_kind"] == "text"
        uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        assert re.fullmatch(rf"raw_intake_{uuid_pattern}", result["intake_public_id"])
        assert re.fullmatch(rf"parser_output_{uuid_pattern}", result["proposal_public_id"])
        assert result["parse_status"] == "parsed_pending_confirmation"
        assert result["final_transaction_created"] is False
        assert outcome.response["idempotent_replay"] is False
        assert outcome.response["operation_id"].startswith("op_")

        conn = support.open_database(workspace)
        try:
            intake_count = conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
            evidence_count = conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0]
            assert intake_count == 1
            assert evidence_count >= 1
            raw_input = conn.execute("SELECT raw_input FROM raw_intake_records").fetchone()[
                "raw_input"
            ]
            assert raw_input == "coffee 6.40"
        finally:
            conn.close()

    def test_identical_replay_returns_original_result(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("taxi 15")
        request = support.make_request(
            "capture",
            support.capture_text_arguments(workspace, update),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.exit_code == bridge_errors.EXIT_OK
        assert second.exit_code == bridge_errors.EXIT_OK
        assert second.response["idempotent_replay"] is True
        assert (
            second.response["result"]["intake_public_id"]
            == first.response["result"]["intake_public_id"]
        )
        assert second.response["operation_id"] == first.response["operation_id"]

        conn = support.open_database(workspace)
        try:
            count = conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_same_telegram_identity_with_different_text_conflicts(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        first = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(
                    workspace, support.telegram_text_update("original text")
                ),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert first.exit_code == bridge_errors.EXIT_OK
        conflicting = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(
                    workspace, support.telegram_text_update("different text")
                ),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert conflicting.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
        assert conflicting.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    def test_unsupported_update_type_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        payload = support.telegram_text_update("hello")
        payload["edited_message"] = payload["message"]
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(workspace, payload),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.UNSUPPORTED_UPDATE_TYPE

    def test_malformed_telegram_update_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "capture",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "kind": "text",
                    "telegram_update": {"update_id": 1},
                },
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert outcome.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED

    def test_unknown_capture_kind_is_refused(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "capture",
                {"workspace_path": str(workspace.workspace_path), "kind": "audio"},
                idempotency_key="capture-key-6",
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED

    def test_concurrent_same_key_capture_creates_one_intake(
        self, workspace: support.BridgeWorkspace
    ) -> None:
        update = support.telegram_text_update("concurrent capture")
        request = support.make_request(
            "capture",
            support.capture_text_arguments(workspace, update),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
        outcomes: list[support.CliOutcome] = []
        lock = threading.Lock()

        def worker() -> None:
            outcome = support.run_cli(request)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(outcome.exit_code == bridge_errors.EXIT_OK for outcome in outcomes)
        public_ids = {outcome.response["result"]["intake_public_id"] for outcome in outcomes}
        assert len(public_ids) == 1
        proposal_ids = {outcome.response["result"]["proposal_public_id"] for outcome in outcomes}
        assert len(proposal_ids) == 1
        # At least one invocation performed the original insert; any other
        # invocation replayed through raw-intake key-level idempotency.
        replay_flags = [outcome.response["idempotent_replay"] for outcome in outcomes]
        assert replay_flags.count(False) >= 1

        conn = support.open_database(workspace)
        try:
            count = conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Deterministic identities, deadline, no-network, no-credential
# ---------------------------------------------------------------------------


class TestCrossCuttingContract:
    def test_operation_identity_is_deterministic(self, workspace: support.BridgeWorkspace) -> None:
        request = support.make_request(
            "health", support.health_arguments(workspace), request_id=support.new_request_id()
        )
        first = support.run_cli(request)
        second = support.run_cli(request)
        assert first.response["operation_id"] == second.response["operation_id"]

    def test_deadline_exceeded_reports_exit_7(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request("health", support.health_arguments(workspace)),
            deadline_seconds=0.0,
        )
        assert outcome.exit_code == bridge_errors.EXIT_DEADLINE_EXCEEDED
        assert outcome.response["error"]["code"] == bridge_errors.DEADLINE_EXCEEDED
        assert outcome.response["error"]["retryable"] is True

    def test_no_network_and_no_credential_execution(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse_socket(*args: object, **kwargs: object) -> None:
            raise AssertionError("Bridge attempted a network socket")

        monkeypatch.setattr(socket, "socket", refuse_socket)
        monkeypatch.setenv("HTTP_PROXY", "")

        update = support.telegram_text_update("offline capture 9.99")
        outcome = support.run_cli(
            support.make_request(
                "capture",
                support.capture_text_arguments(workspace, update),
                idempotency_key=support.canonical_capture_key(message_id=10),
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_OK

        status = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": outcome.response["result"]["intake_public_id"],
                },
            )
        )
        assert status.exit_code == bridge_errors.EXIT_OK

    def test_bridge_package_has_no_transport_or_credential_surface(self) -> None:
        import finance_core.openclaw_staging_bridge as bridge_package

        package_dir = Path(bridge_package.__file__).resolve().parent
        forbidden_fragments = (
            "telegram_bot_api_transport",
            "TelegramBotApiTransport",
            "bot_token",
            "urllib",
            "requests.",
            "http.client",
        )
        for source_file in package_dir.glob("*.py"):
            source_text = source_file.read_text(encoding="utf-8")
            for fragment in forbidden_fragments:
                assert fragment not in source_text, (
                    f"{source_file.name} references forbidden surface {fragment!r}"
                )

    def test_stderr_diagnostics_are_bounded(self, workspace: support.BridgeWorkspace) -> None:
        outcome = support.run_cli(
            support.make_request(
                "get_status",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "intake_public_id": "raw_intake_missing",
                },
            )
        )
        assert outcome.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert len(outcome.stderr) <= 600
