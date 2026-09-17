"""Production OCR resolver boundary tests for the staging bridge propose path.

The receipt propose path resolves its OCR engine exclusively through the
trusted workspace runtime boundary (``runtime/ocr_engine.json``); the
request envelope can never supply or influence the executable.  These tests
exercise the resolver contract without installing a helper, configuring
OpenClaw, or touching credentials: a deterministic fake helper implements
the native ``--identity``/``--ocr`` subprocess protocol, and the committed
Swift helper is compiled on Darwin arm64 hosts only.  All data is temporary
and synthetic.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import textwrap
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.macos_vision_receipt_ocr import (
    HELPER_NAME,
    HELPER_PROTOCOL_VERSION,
    RECOGNITION_LEVEL,
    USES_LANGUAGE_CORRECTION,
    VISION_REQUEST_REVISION,
)
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import ocr_boundary

EXPECTED_VERSION = "1.0.0"
FAKE_SUPPORTED_LANGUAGES = ["en-US", "ms-MY", "zh-Hans"]

REPO_ROOT = Path(__file__).resolve().parents[1]
SWIFT_SOURCE = REPO_ROOT / "native" / "macos_vision_receipt_ocr" / "main.swift"

IS_DARWIN_ARM64 = sys.platform == "darwin" and platform.machine() == "arm64"
darwin_arm64 = pytest.mark.skipif(
    not IS_DARWIN_ARM64,
    reason="The real Apple Vision helper requires Darwin on Apple Silicon arm64.",
)


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


@pytest.fixture()
def vision_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the Darwin gate so the fake-helper protocol runs anywhere."""
    from finance_core.intake import macos_vision_receipt_ocr as mv

    monkeypatch.setattr(mv, "_require_macos_vision_platform", lambda: None)
    monkeypatch.setattr(mv, "_observe_child_resident_bytes", lambda pid: 0)


def identity_literal(*, helper_version: str = EXPECTED_VERSION) -> str:
    return json.dumps(
        {
            "protocol_version": HELPER_PROTOCOL_VERSION,
            "helper_name": HELPER_NAME,
            "helper_version": helper_version,
            "vision_request_revision": VISION_REQUEST_REVISION,
            "recognition_level": RECOGNITION_LEVEL,
            "uses_language_correction": USES_LANGUAGE_CORRECTION,
            "supported_languages": list(FAKE_SUPPORTED_LANGUAGES),
        }
    )


def ocr_literal() -> str:
    return json.dumps(
        {
            "protocol_version": HELPER_PROTOCOL_VERSION,
            "status": "ok",
            "outcome_code": "ok",
            "page_width": 480,
            "page_height": 640,
            "orientation": 1,
            "observations": [
                {
                    "index": 0,
                    "text": "TOTAL 12.34",
                    "confidence": 0.987654,
                    "bounding_box": [0.25, 0.60, 0.50, 0.06],
                }
            ],
        }
    )


def write_fake_helper(
    directory: Path, *, identity: str | None = None, name: str = "trusted-ocr-helper"
) -> Path:
    """Write an executable fake helper implementing the native protocol."""
    path = (directory / name).resolve()
    body = f"""
import os
import sys

args = sys.argv[1:]
if args[:1] == ['--identity']:
    os.write(1, {(identity if identity is not None else identity_literal())!r}.encode('utf-8'))
    raise SystemExit(0)
if args[:1] == ['--ocr']:
    fd = int(args[1])
    max_bytes = int(args[2])
    data = os.read(fd, max_bytes) if fd >= 0 else b''
    os.write(1, {ocr_literal()!r}.encode('utf-8'))
    raise SystemExit(0)
raise SystemExit(2)
"""
    script = f"#!{sys.executable}\n" + textwrap.dedent(body)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o500)
    return path


def write_engine_config(workspace: support.BridgeWorkspace, payload: dict[str, object]) -> Path:
    config_path = workspace.workspace_path / "runtime" / ocr_boundary.OCR_ENGINE_CONFIG_FILENAME
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def valid_config(helper_path: Path) -> dict[str, object]:
    return {
        "schema_version": ocr_boundary.OCR_ENGINE_CONFIG_SCHEMA_VERSION,
        "helper_path": str(helper_path),
        "expected_version": EXPECTED_VERSION,
    }


def resolver_refusal(workspace: support.BridgeWorkspace) -> bridge_errors.BridgeError:
    with pytest.raises(bridge_errors.BridgeError) as excinfo:
        ocr_boundary.resolve_workspace_ocr_engine(workspace.workspace_path)
    exc = excinfo.value
    assert exc.code == bridge_errors.OCR_ENGINE_UNAVAILABLE
    assert exc.retryable is False
    return exc


class TestTrustedResolverConfiguration:
    def test_missing_configuration_fails_closed(
        self, workspace: support.BridgeWorkspace, vision_platform: None
    ) -> None:
        exc = resolver_refusal(workspace)
        assert "request envelope" in str(exc)

    def test_symlinked_configuration_fails_closed(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        real_config = tmp_path / "real_config.json"
        real_config.write_text(json.dumps(valid_config(helper)), encoding="utf-8")
        config_path = workspace.workspace_path / "runtime" / ocr_boundary.OCR_ENGINE_CONFIG_FILENAME
        config_path.symlink_to(real_config)
        resolver_refusal(workspace)

    def test_relative_helper_path_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None
    ) -> None:
        config = valid_config(Path("relative/helper"))
        write_engine_config(workspace, config)
        resolver_refusal(workspace)

    def test_missing_helper_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        write_engine_config(workspace, valid_config(tmp_path / "absent-helper"))
        resolver_refusal(workspace)

    def test_unknown_schema_version_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        config = valid_config(helper)
        config["schema_version"] = "v999"
        write_engine_config(workspace, config)
        resolver_refusal(workspace)

    def test_malformed_expected_version_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        config = valid_config(helper)
        config["expected_version"] = "bad version!"
        write_engine_config(workspace, config)
        resolver_refusal(workspace)

    def test_symlinked_helper_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        real_helper = write_fake_helper(tmp_path, name="real-helper")
        link = tmp_path / "linked-helper"
        link.symlink_to(real_helper)
        write_engine_config(workspace, valid_config(link))
        resolver_refusal(workspace)

    def test_non_executable_helper_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        helper.chmod(0o400)
        write_engine_config(workspace, valid_config(helper))
        resolver_refusal(workspace)

    def test_group_writable_helper_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        helper.chmod(0o550)
        write_engine_config(workspace, valid_config(helper))
        resolver_refusal(workspace)

    def test_helper_version_mismatch_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)  # reports 1.0.0
        config = valid_config(helper)
        config["expected_version"] = "2.0.0"
        write_engine_config(workspace, config)
        resolver_refusal(workspace)

    def test_pinned_binary_hash_mismatch_is_refused(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        config = valid_config(helper)
        config["binary_sha256"] = "0" * 64
        write_engine_config(workspace, config)
        resolver_refusal(workspace)

    def test_valid_configuration_resolves_verified_engine(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        helper = write_fake_helper(tmp_path)
        config = valid_config(helper)
        config["binary_sha256"] = support.sha256_hex(helper.read_bytes())
        write_engine_config(workspace, config)
        engine = ocr_boundary.resolve_workspace_ocr_engine(workspace.workspace_path)
        assert engine.identity.name == "macos_vision"
        assert engine.identity.version == EXPECTED_VERSION
        assert engine.identity.binary_sha256 == support.sha256_hex(helper.read_bytes())


class TestProductionProposePath:
    def _capture_receipt(self, workspace: support.BridgeWorkspace) -> str:
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        capture = support.run_cli(
            support.make_request(
                "capture",
                support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
                idempotency_key=support.canonical_capture_key(message_id=20),
            )
        )
        assert capture.exit_code == bridge_errors.EXIT_OK, capture.response
        return str(capture.response["result"]["intake_public_id"])

    def test_propose_succeeds_through_trusted_resolver_without_monkeypatch(
        self, workspace: support.BridgeWorkspace, vision_platform: None, tmp_path: Path
    ) -> None:
        # The production success path: no factory monkeypatch.  The engine is
        # resolved from the trusted runtime configuration and satisfies the
        # real subprocess identity protocol.
        assert ocr_boundary.engine_factory is ocr_boundary.resolve_workspace_ocr_engine
        helper = write_fake_helper(tmp_path)
        write_engine_config(workspace, valid_config(helper))
        intake_public_id = self._capture_receipt(workspace)
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
        result = propose.response["result"]
        assert result["proposal_public_id"].startswith("prop_bridge_")
        assert result["parse_status"] == "parsed_pending_confirmation"
        assert result["final_transaction_created"] is False

    def test_propose_fails_closed_without_configuration_and_preserves_evidence(
        self, workspace: support.BridgeWorkspace, vision_platform: None
    ) -> None:
        intake_public_id = self._capture_receipt(workspace)
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
        assert propose.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
        assert propose.response["error"]["code"] == bridge_errors.OCR_ENGINE_UNAVAILABLE
        assert propose.response["error"]["retryable"] is False

        conn = support.open_database(workspace)
        try:
            assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
            assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        finally:
            conn.close()


@darwin_arm64
class TestRealHelperResolver:
    def test_resolver_accepts_compiled_vision_helper(
        self, workspace: support.BridgeWorkspace, tmp_path: Path
    ) -> None:
        # The committed Swift helper satisfies the resolver contract end to
        # end: absolute path, file safety attributes, --identity protocol,
        # expected version, and the binary SHA-256 identity.
        assert SWIFT_SOURCE.is_file()
        binary = tmp_path / "macos_vision_receipt_ocr"
        module_cache = tmp_path / "module-cache"
        module_cache.mkdir()
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-O",
                "-swift-version",
                "5",
                "-module-cache-path",
                str(module_cache),
                "-o",
                str(binary),
                str(SWIFT_SOURCE),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        binary.chmod(0o500)
        config = valid_config(binary)
        config["binary_sha256"] = support.sha256_hex(binary.read_bytes())
        write_engine_config(workspace, config)
        engine = ocr_boundary.resolve_workspace_ocr_engine(workspace.workspace_path)
        assert engine.identity.name == "macos_vision"
        assert engine.identity.version == EXPECTED_VERSION
        assert engine.identity.binary_sha256 == support.sha256_hex(binary.read_bytes())
