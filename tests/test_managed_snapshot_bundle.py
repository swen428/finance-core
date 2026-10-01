"""Synthetic proofs for complete, independently verified Core snapshot bundles."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import openclaw_staging_bridge_support_v1 as bridge_support
import pytest

import finance_core
from finance_core.managed_cut_protocol import (
    BUNDLE_LIMITS,
    BUNDLE_LIMITS_VERSION,
    BUNDLE_REGISTRY_VERSION,
    BUNDLE_SCOPE,
    BUNDLE_VERSION,
    CutRequest,
    validate_request,
)
from finance_core.managed_disk_snapshot import (
    DiskSnapshotLimits,
    stage_disk_snapshot,
)
from finance_core.managed_snapshot_bundle import (
    BundleError,
    canonical_json,
    collect_references,
    stage_bundle,
    verify_bundle,
)
from finance_core.managed_staging_profile import _delegated_cut_source
from finance_core.openclaw_staging_bridge import workspace_access
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import ManagedStagingProfile, _migration_contract_digest
from tests import test_s1c_a_managed_bridge_commands as managed_commands
from tests import test_s3a_managed_capture_publication as capture_tests

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)


@pytest.fixture()
def captured_receipt_workspace(
    managed_workspace: managed_commands.ManagedBridgeWorkspace,
) -> managed_commands.ManagedBridgeWorkspace:
    """Create a real managed capture and immutable attachment publication."""
    handoff = capture_tests._write_managed_handoff(
        managed_workspace, "bundle-receipt.jpg", bridge_support.JPEG_BYTES
    )
    request = capture_tests._receipt_request(
        managed_workspace, message_id=9301, filename=handoff.name
    )
    outcome = managed_commands._run(managed_workspace, request, expected_sessions=1)
    assert outcome.exit_code == 0, outcome.response
    assert outcome.response["result"]["capture_kind"] == "receipt_image"
    assert outcome.response["result"]["capture_job"]["status"] == "captured"
    return managed_workspace


def _write_canonical_jpeg(workspace_path: Path, content: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(content).hexdigest()
    relative = f"{digest[:2]}/{digest}.jpg"
    target = workspace_path / "attachments" / relative
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(0o400)
    return relative, target


def _add_unused_attachment(
    workspace: managed_commands.ManagedBridgeWorkspace,
    *,
    public_id: str = "att_bundle_unused",
    content: bytes = b"\xff\xd8\xffsynthetic-unused-bundle-member",
) -> tuple[str, Path, str]:
    """Add a schema-valid declaration with no consumers to the synthetic profile."""
    relative, path = _write_canonical_jpeg(workspace.workspace_path, content)
    digest = hashlib.sha256(content).hexdigest()
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-bundle-add-unused-attachment"
    ) as connection:
        connection.execute(
            """
            INSERT INTO attachments (
                public_id, attachment_type, file_path, original_filename,
                mime_type, file_hash, source_channel
            ) VALUES (?, 'telegram_attachment', ?, ?, 'image/jpeg', ?, 'telegram')
            """,
            (public_id, str(path), "unused.jpg", digest),
        )
        connection.commit()
    return relative, path, digest


def _attachment_rows(
    workspace: managed_commands.ManagedBridgeWorkspace,
) -> list[tuple[int, str, str | None, str]]:
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-bundle-read-attachments"
    ) as connection:
        return [
            (int(row[0]), str(row[1]), row[2], str(row[3]))
            for row in connection.execute(
                "SELECT id, public_id, file_hash, file_path FROM attachments ORDER BY id"
            )
        ]


def _add_historical_attachment_reference(
    workspace: managed_commands.ManagedBridgeWorkspace,
    *,
    public_id: str,
    content: bytes,
    attachment_hash: str | None,
    evidence_hash: str | None = None,
    evidence_source_hash: str | None = None,
    reference_path: str | None = None,
    with_attachment: bool = True,
) -> tuple[Path, str]:
    """Model an old synthetic source row with the migration's nullable hash fields."""
    canonical_relative, canonical_path = _write_canonical_jpeg(workspace.workspace_path, content)
    digest = hashlib.sha256(content).hexdigest()
    canonical_path_value = str(canonical_path)
    selected_path = canonical_path_value if reference_path is None else reference_path
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-bundle-seed-historical-reference"
    ) as connection:
        attachment_id: int | None = None
        if with_attachment:
            cursor = connection.execute(
                """
                INSERT INTO attachments (
                    public_id, attachment_type, file_path, original_filename,
                    mime_type, file_hash, source_channel
                ) VALUES (?, 'telegram_attachment', ?, ?, 'image/jpeg', ?, 'telegram')
                """,
                (
                    f"att_{public_id}",
                    canonical_path_value,
                    f"{public_id}.jpg",
                    attachment_hash,
                ),
            )
            assert cursor.lastrowid is not None
            attachment_id = cursor.lastrowid

        intake_cursor = connection.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_id, attachment_path, attachment_hash
            ) VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z', ?, ?, ?)
            """,
            (
                f"intake_{public_id}",
                f"synthetic historical attachment {public_id}",
                attachment_id,
                selected_path,
                evidence_hash if evidence_hash is not None else attachment_hash,
            ),
        )
        assert intake_cursor.lastrowid is not None
        raw_intake_id = intake_cursor.lastrowid
        if evidence_hash is not None or evidence_source_hash is not None:
            connection.execute(
                """
                INSERT INTO raw_intake_evidence (
                    public_id, raw_intake_record_id, attachment_id, evidence_type,
                    attachment_path, source_file_hash, attachment_hash
                ) VALUES (?, ?, ?, 'attachment', ?, ?, ?)
                """,
                (
                    f"evidence_{public_id}",
                    raw_intake_id,
                    attachment_id,
                    selected_path,
                    evidence_source_hash,
                    evidence_hash,
                ),
            )
        connection.commit()
    assert canonical_relative.endswith(f"{digest}.jpg")
    return canonical_path, digest


def _collect(workspace: managed_commands.ManagedBridgeWorkspace):
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-bundle-collect-references"
    ) as connection:
        return collect_references(
            connection,
            workspace.workspace_path / "attachments",
            BUNDLE_LIMITS,
            time.monotonic() + 10.0,
            lambda: None,
        )


def _bundle_request(profile: ManagedStagingProfile, cut_id: str):
    registration = profile.registration.read_bytes()
    limits_digest = hashlib.sha256(canonical_json(BUNDLE_LIMITS)).hexdigest()
    return validate_request(
        {
            "version": BUNDLE_VERSION,
            "cut_id": cut_id,
            "worker_id": uuid.uuid4().hex,
            "profile_id": profile.profile_id,
            "registration_sha256": hashlib.sha256(registration).hexdigest(),
            "artifact_sha256": "a" * 64,
            "schema_sha256": _migration_contract_digest(),
            "limits_sha256": limits_digest,
            "remaining_ms": 20_000,
            "limits": dict(BUNDLE_LIMITS),
            "operation": "core_bundle_snapshot",
            "scope": BUNDLE_SCOPE,
            "registry_version": BUNDLE_REGISTRY_VERSION,
            "limits_version": BUNDLE_LIMITS_VERSION,
            "core_version": importlib.metadata.version("finance-core"),
            "core_api_contract_version": finance_core.API_CONTRACT_VERSION,
        },
        expected_operation="core_bundle_snapshot",
    )


def _stage_bundle(
    workspace: managed_commands.ManagedBridgeWorkspace,
) -> tuple[Path, CutRequest, dict[str, object]]:
    profile = workspace.witness
    assert isinstance(profile, ManagedStagingProfile)
    cut_id = uuid.uuid4().hex
    stage = profile.work / f"core-cut-{cut_id}"
    stage.mkdir(mode=0o700)
    db_stage = stage / "db"
    db_stage.mkdir(mode=0o700)
    request = _bundle_request(profile, cut_id)
    deadline = time.monotonic() + 20.0
    disk_limits = DiskSnapshotLimits(
        max_core_db_bytes=BUNDLE_LIMITS["max_core_db_bytes"],
        max_stage_bytes=BUNDLE_LIMITS["max_db_stage_bytes"],
        min_free_bytes=BUNDLE_LIMITS["min_free_bytes"],
        backup_pages_per_step=BUNDLE_LIMITS["backup_pages_per_step"],
    )
    snapshot_recorded_at = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    with exclusive_cut(profile):
        with _delegated_cut_source(profile) as source:
            staged_disk = stage_disk_snapshot(
                source,
                private_stage=db_stage,
                limits=disk_limits,
                deadline_monotonic=deadline,
                _control_check=lambda: None,
            )
        evidence = stage_bundle(
            profile=profile,
            stage=stage,
            request=request,
            staged_disk=staged_disk,
            snapshot_recorded_at=snapshot_recorded_at,
            deadline=deadline,
            control_check=lambda: None,
        )
    return stage, request, evidence


def _reader_request(request: CutRequest, evidence: dict[str, object]) -> CutRequest:
    return replace(request, operation="core_bundle_readback", staged=dict(evidence))


def _rewrite_manifest(
    stage: Path, manifest: dict[str, object], evidence: dict[str, object]
) -> dict[str, object]:
    encoded = canonical_json(manifest)
    path = stage / "manifest.json"
    path.write_bytes(encoded)
    path.chmod(0o600)
    updated = dict(evidence)
    updated["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    updated["manifest_bytes"] = len(encoded)
    if "member_count" in manifest:
        updated["member_count"] = manifest["member_count"]
    if "member_bytes" in manifest:
        updated["member_bytes"] = manifest["member_bytes"]
    return updated


def test_registry_closes_over_capture_lineage_and_declared_unused_attachment(
    captured_receipt_workspace: managed_commands.ManagedBridgeWorkspace,
) -> None:
    _, unused_path, unused_hash = _add_unused_attachment(captured_receipt_workspace)

    inventory = _collect(captured_receipt_workspace)

    assert len(inventory.members) == 2
    assert any(path.endswith(f"{unused_hash}.jpg") for path in inventory.members)
    assert unused_path.is_file()
    assert (
        inventory.digest
        == hashlib.sha256(
            b"".join(
                payload + b"\n"
                for payload in sorted(canonical_json(fact) for fact in inventory.facts)
            )
        ).hexdigest()
    )
    tables = {fact["table"] for fact in inventory.facts}
    assert {
        "attachments",
        "raw_intake_records",
        "raw_intake_evidence",
        "telegram_attachment_source",
        "finance_capture_jobs",
    } <= tables
    assert any(fact["member"].endswith(f"{unused_hash}.jpg") for fact in inventory.facts)
    assert (
        sum(
            fact["member"] != f"attachments/{unused_hash[:2]}/{unused_hash}.jpg"
            for fact in inventory.facts
        )
        > 1
    ), "the captured original must retain its distinct evidence lineage facts"


def test_registry_uses_linked_existing_hash_when_attachment_file_hash_is_null(
    managed_workspace: managed_commands.ManagedBridgeWorkspace,
) -> None:
    _, digest = _add_historical_attachment_reference(
        managed_workspace,
        public_id="null-file-hash",
        content=b"\xff\xd8\xffhistorical-reference-with-trusted-evidence-hash",
        attachment_hash=None,
        evidence_hash=None,
    )
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-read-linked-existing-hash"
    ) as connection:
        attachment_id = connection.execute(
            "SELECT id FROM attachments WHERE public_id = ?", ("att_null-file-hash",)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO raw_intake_evidence (
                public_id, raw_intake_record_id, attachment_id, evidence_type,
                attachment_path, attachment_hash, source_file_hash
            ) SELECT ?, id, ?, 'attachment', attachment_path, ?, NULL
                FROM raw_intake_records WHERE public_id = ?
            """,
            (
                "evidence_null-file-hash",
                attachment_id,
                digest,
                "intake_null-file-hash",
            ),
        )
        connection.commit()

    inventory = _collect(managed_workspace)

    member = f"attachments/{digest[:2]}/{digest}.jpg"
    assert inventory.members[member][0] == digest
    assert any(
        fact["table"] == "raw_intake_evidence" and fact["member"] == member
        for fact in inventory.facts
    )


@pytest.mark.parametrize(
    ("reference_kind", "message"),
    (
        ("no_hash", "unique authoritative hash"),
        ("conflict", "hashes conflict"),
        ("outside_root", "outside the fixed managed root"),
    ),
)
def test_registry_rejects_untrusted_or_ambiguous_attachment_references(
    managed_workspace: managed_commands.ManagedBridgeWorkspace,
    tmp_path: Path,
    reference_kind: str,
    message: str,
) -> None:
    content = b"\xff\xd8\xffsynthetic-invalid-history-reference"
    digest = hashlib.sha256(content).hexdigest()
    kwargs: dict[str, object] = {
        "public_id": f"invalid-{reference_kind}",
        "content": content,
        "attachment_hash": digest,
    }
    if reference_kind == "no_hash":
        kwargs["attachment_hash"] = None
    elif reference_kind == "conflict":
        kwargs["evidence_hash"] = digest
        kwargs["evidence_source_hash"] = "f" * 64
    else:
        kwargs.update(
            with_attachment=False,
            reference_path=str(tmp_path / "external" / "attachment.jpg"),
            evidence_hash=digest,
        )
    _add_historical_attachment_reference(managed_workspace, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(BundleError, match=message):
        _collect(managed_workspace)


@pytest.fixture()
def captured_bundle(
    captured_receipt_workspace: managed_commands.ManagedBridgeWorkspace,
) -> tuple[Path, CutRequest, dict[str, object]]:
    _add_unused_attachment(captured_receipt_workspace)
    return _stage_bundle(captured_receipt_workspace)


def test_real_registered_bundle_passes_independent_fresh_database_readback(
    captured_bundle: tuple[Path, CutRequest, dict[str, object]],
) -> None:
    stage, request, evidence = captured_bundle
    manifest = json.loads((stage / "manifest.json").read_bytes())

    verified = verify_bundle(
        stage=stage,
        request=_reader_request(request, evidence),
        deadline=time.monotonic() + 10.0,
        control_check=lambda: None,
    )

    assert manifest["scope"] == BUNDLE_SCOPE
    assert manifest["reference_registry_version"] == BUNDLE_REGISTRY_VERSION
    assert manifest["limits_version"] == BUNDLE_LIMITS_VERSION
    assert manifest["member_count"] == 3
    assert manifest["reference_count"] > 1
    assert verified["tree_identity_sha256"]
    assert verified["journal_mode"] == "delete"
    member_paths = [member["path"] for member in manifest["members"]]
    assert member_paths == sorted(member_paths)
    assert member_paths.count("db/core.sqlite") == 1
    assert sum(path.startswith("attachments/") for path in member_paths) == 2


@pytest.mark.parametrize(
    "corruption",
    (
        "tampered_member",
        "symlink_member",
        "extra_member",
        "malformed_manifest",
        "omitted_reference",
    ),
)
def test_fresh_readback_rejects_changed_or_incomplete_bundle_tree(
    captured_bundle: tuple[Path, CutRequest, dict[str, object]],
    corruption: str,
) -> None:
    stage, request, original_evidence = captured_bundle
    manifest_path = stage / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    evidence = dict(original_evidence)
    attachment_member = next(
        member["path"] for member in manifest["members"] if member["role"] == "attachment"
    )
    attachment_path = stage / attachment_member

    if corruption == "tampered_member":
        attachment_path.chmod(0o600)
        attachment_path.write_bytes(attachment_path.read_bytes() + b"tampered")
        attachment_path.chmod(0o400)
    elif corruption == "symlink_member":
        original_path = request_stage_workspace_attachment_root(
            stage
        ) / attachment_member.removeprefix("attachments/")
        attachment_path.unlink()
        attachment_path.symlink_to(original_path)
    elif corruption == "extra_member":
        extra_path = attachment_path.with_name("extra-member.jpg")
        extra_path.write_bytes(b"\xff\xd8\xffunexpected-extra-member")
        extra_path.chmod(0o400)
    elif corruption == "malformed_manifest":
        malformed = b"{"
        manifest_path.chmod(0o600)
        manifest_path.write_bytes(malformed)
        evidence["manifest_sha256"] = hashlib.sha256(malformed).hexdigest()
        evidence["manifest_bytes"] = len(malformed)
    else:
        removed = next(item for item in manifest["members"] if item["role"] == "attachment")
        manifest["members"].remove(removed)
        manifest["member_count"] -= 1
        manifest["member_bytes"] -= removed["bytes"]
        evidence = _rewrite_manifest(stage, manifest, evidence)
        (stage / removed["path"]).unlink()

    with pytest.raises((BundleError, OSError)):
        verify_bundle(
            stage=stage,
            request=_reader_request(request, evidence),
            deadline=time.monotonic() + 10.0,
            control_check=lambda: None,
        )


def request_stage_workspace_attachment_root(stage: Path) -> Path:
    """Return the registered workspace attachment root for this fixed stage."""
    return stage.parent.parent / "workspace" / "attachments"
