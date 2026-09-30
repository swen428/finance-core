"""Fixed synthetic Core snapshot worker, invoked only by ManagedCutCoordinator."""

from __future__ import annotations

import os

from finance_core.managed_cut_protocol import (
    ManagedCutProtocolError,
    check_control_alive,
    initial_handshake,
    write_frame,
)
from finance_core.managed_disk_snapshot import DiskSnapshotLimits, stage_disk_snapshot
from finance_core.managed_staging_profile import _delegated_cut_source
from finance_core.profile_paths import _migration_contract_digest


def main() -> int:
    profile = None
    try:
        request, profile, stage, deadline = initial_handshake("core_snapshot")
        if (
            request.schema_sha256 != _migration_contract_digest()
            or request.artifact_sha256 != os.environ.get("FINANCE_CUT_ARTIFACT_SHA256")
        ):
            raise ManagedCutProtocolError("Installed cut identity differs")
        limits = DiskSnapshotLimits(**request.limits)
        limits.validate()
        check_control_alive(deadline)
        with _delegated_cut_source(profile) as source:
            staged = stage_disk_snapshot(
                source,
                private_stage=stage,
                limits=limits,
                deadline_monotonic=deadline,
                _control_check=lambda: check_control_alive(deadline),
            )
        check_control_alive(deadline)
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
            write_frame({"version": "delegated-cut-worker-v1", "type": "failed"})
        except BaseException:
            pass
        return 2
    finally:
        if profile is not None:
            profile.close()


if __name__ == "__main__":
    raise SystemExit(main())
