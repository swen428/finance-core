"""Fresh fixed-process SQLite readback; this entry never spawns descendants."""

from __future__ import annotations

import os

from finance_core.managed_cut_protocol import (
    BUNDLE_VERSION,
    ManagedCutProtocolError,
    canonical_decimal,
    check_control_alive,
    failure_version,
    initial_handshake,
    write_frame,
)
from finance_core.managed_disk_snapshot import (
    DiskSnapshotLimits,
    StagedDiskSnapshot,
    verify_staged_disk_snapshot,
)
from finance_core.managed_snapshot_bundle import installed_version_matches, verify_bundle
from finance_core.profile_paths import _migration_contract_digest


def main() -> int:
    profile = None
    request = None
    try:
        request, profile, stage, deadline = initial_handshake(
            ("core_readback", "core_bundle_readback")
        )
        if (
            request.schema_sha256 != _migration_contract_digest()
            or request.artifact_sha256 != os.environ.get("FINANCE_CUT_ARTIFACT_SHA256")
        ):
            raise ManagedCutProtocolError("Installed cut identity differs")
        assert request.staged is not None
        evidence = request.staged
        if request.operation == "core_bundle_readback":
            installed_version_matches(request)
            db_stage = stage / "db"
            staged = StagedDiskSnapshot(
                output=str(db_stage / "core.sqlite"),
                byte_length=evidence["db_bytes"],
                sha256=evidence["db_sha256"],
                page_count=evidence["db_page_count"],
                stage_dev=canonical_decimal(evidence["db_stage_dev"]),
                stage_ino=canonical_decimal(evidence["db_stage_ino"]),
                output_dev=canonical_decimal(evidence["db_output_dev"]),
                output_ino=canonical_decimal(evidence["db_output_ino"]),
            )
            limits = DiskSnapshotLimits(
                max_core_db_bytes=request.limits["max_core_db_bytes"],
                max_stage_bytes=request.limits["max_db_stage_bytes"],
                min_free_bytes=request.limits["min_free_bytes"],
                backup_pages_per_step=request.limits["backup_pages_per_step"],
            )
        else:
            db_stage = stage
            staged = StagedDiskSnapshot(
                output=str(stage / "core.sqlite"),
                byte_length=evidence["byte_length"],
                sha256=evidence["sha256"],
                page_count=evidence["page_count"],
                stage_dev=canonical_decimal(evidence["stage_dev"]),
                stage_ino=canonical_decimal(evidence["stage_ino"]),
                output_dev=canonical_decimal(evidence["output_dev"]),
                output_ino=canonical_decimal(evidence["output_ino"]),
            )
            limits = DiskSnapshotLimits(**request.limits)
        check_control_alive(deadline)
        receipt = verify_staged_disk_snapshot(
            staged,
            private_stage=db_stage,
            limits=limits,
            deadline_monotonic=deadline,
            _direct_reader=True,
            _control_check=lambda: check_control_alive(deadline),
        )
        check_control_alive(deadline)
        if request.operation == "core_bundle_readback":
            if (
                receipt.byte_length != evidence["db_bytes"]
                or receipt.sha256 != evidence["db_sha256"]
            ):
                raise ManagedCutProtocolError("Bundle database readback differs")
            bundle = verify_bundle(
                stage=stage,
                request=request,
                deadline=deadline,
                control_check=lambda: check_control_alive(deadline),
            )
            write_frame(
                {
                    "version": BUNDLE_VERSION,
                    "type": "bundle_readback_verified",
                    "cut_id": request.cut_id,
                    "worker_id": request.worker_id,
                    "profile_id": request.profile_id,
                    "operation": request.operation,
                    "artifact_sha256": request.artifact_sha256,
                    "schema_sha256": request.schema_sha256,
                    "scope": request.scope,
                    "registry_version": request.registry_version,
                    "limits_version": request.limits_version,
                    "core_version": request.core_version,
                    "core_api_contract_version": request.core_api_contract_version,
                    **bundle,
                    "reader_closed": True,
                }
            )
            return 0
        write_frame(
            {
                "version": "delegated-cut-worker-v1",
                "type": "verified",
                "cut_id": request.cut_id,
                "worker_id": request.worker_id,
                "profile_id": request.profile_id,
                "operation": request.operation,
                "artifact_sha256": request.artifact_sha256,
                "schema_sha256": request.schema_sha256,
                "output_role": "core.sqlite",
                "byte_length": receipt.byte_length,
                "sha256": receipt.sha256,
                "page_count": receipt.page_count,
                "schema_object_count": receipt.schema_object_count,
                "journal_mode": receipt.journal_mode,
                "reader_closed": True,
            }
        )
        return 0
    except BaseException:
        try:
            write_frame({"version": failure_version(), "type": "failed"})
        except BaseException:
            pass
        return 2
    finally:
        if profile is not None:
            profile.close()


if __name__ == "__main__":
    raise SystemExit(main())
