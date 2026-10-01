"""Fixed synthetic Core snapshot worker, invoked only by ManagedCutCoordinator."""

from __future__ import annotations

import os

from finance_core.managed_cut_protocol import (
    BUNDLE_VERSION,
    ManagedCutProtocolError,
    check_control_alive,
    failure_version,
    initial_handshake,
    write_frame,
)
from finance_core.managed_disk_snapshot import DiskSnapshotLimits, stage_disk_snapshot
from finance_core.managed_snapshot_bundle import _utc_now, installed_version_matches, stage_bundle
from finance_core.managed_staging_profile import _delegated_cut_source
from finance_core.profile_paths import _migration_contract_digest


def main() -> int:
    profile = None
    request = None
    try:
        request, profile, stage, deadline = initial_handshake(
            ("core_snapshot", "core_bundle_snapshot")
        )
        if (
            request.schema_sha256 != _migration_contract_digest()
            or request.artifact_sha256 != os.environ.get("FINANCE_CUT_ARTIFACT_SHA256")
        ):
            raise ManagedCutProtocolError("Installed cut identity differs")
        if request.operation == "core_bundle_snapshot":
            installed_version_matches(request)
            limits = DiskSnapshotLimits(
                max_core_db_bytes=request.limits["max_core_db_bytes"],
                max_stage_bytes=request.limits["max_db_stage_bytes"],
                min_free_bytes=request.limits["min_free_bytes"],
                backup_pages_per_step=request.limits["backup_pages_per_step"],
            )
            os.mkdir(stage / "db", 0o700)
            db_stage = stage / "db"
        else:
            limits = DiskSnapshotLimits(**request.limits)
            db_stage = stage
        limits.validate()
        check_control_alive(deadline)
        with _delegated_cut_source(profile) as source:
            snapshot_recorded_at = _utc_now()
            staged = stage_disk_snapshot(
                source,
                private_stage=db_stage,
                limits=limits,
                deadline_monotonic=deadline,
                _control_check=lambda: check_control_alive(deadline),
            )
        check_control_alive(deadline)
        if request.operation == "core_bundle_snapshot":
            bundle = stage_bundle(
                profile=profile,
                stage=stage,
                request=request,
                staged_disk=staged,
                snapshot_recorded_at=snapshot_recorded_at,
                deadline=deadline,
                control_check=lambda: check_control_alive(deadline),
            )
            write_frame(
                {
                    "version": BUNDLE_VERSION,
                    "type": "bundle_staged",
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
                    "backup_complete": True,
                    "source_closed": True,
                }
            )
            return 0
        write_frame(
            {
                "version": "delegated-cut-worker-v1",
                "type": "staged",
                "cut_id": request.cut_id,
                "worker_id": request.worker_id,
                "profile_id": request.profile_id,
                "operation": request.operation,
                "artifact_sha256": request.artifact_sha256,
                "schema_sha256": request.schema_sha256,
                "output_role": "core.sqlite",
                "byte_length": staged.byte_length,
                "sha256": staged.sha256,
                "page_count": staged.page_count,
                "stage_dev": str(staged.stage_dev),
                "stage_ino": str(staged.stage_ino),
                "output_dev": str(staged.output_dev),
                "output_ino": str(staged.output_ino),
                "backup_complete": True,
                "source_closed": True,
            }
        )
        return 0
    except BaseException:
        # No paths, capabilities, SQLite details or exception text cross the
        # bounded control channel. The coordinator retains the failed stage.
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
