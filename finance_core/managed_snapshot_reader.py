"""Fresh fixed-process SQLite readback; this entry never spawns descendants."""

from __future__ import annotations

import os

from finance_core.managed_cut_protocol import (
    ManagedCutProtocolError,
    canonical_decimal,
    check_control_alive,
    initial_handshake,
    write_frame,
)
from finance_core.managed_disk_snapshot import (
    DiskSnapshotLimits,
    StagedDiskSnapshot,
    verify_staged_disk_snapshot,
)
from finance_core.profile_paths import _migration_contract_digest


def main() -> int:
    profile = None
    try:
        request, profile, stage, deadline = initial_handshake("core_readback")
        if (
            request.schema_sha256 != _migration_contract_digest()
            or request.artifact_sha256 != os.environ.get("FINANCE_CUT_ARTIFACT_SHA256")
        ):
            raise ManagedCutProtocolError("Installed cut identity differs")
        assert request.staged is not None
        evidence = request.staged
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
            private_stage=stage,
            limits=limits,
            deadline_monotonic=deadline,
            _direct_reader=True,
        )
        check_control_alive(deadline)
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
            write_frame({"version": "delegated-cut-worker-v1", "type": "failed"})
        except BaseException:
            pass
        return 2
    finally:
        if profile is not None:
            profile.close()


if __name__ == "__main__":
    raise SystemExit(main())
