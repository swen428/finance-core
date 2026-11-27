"""Core-only origin receipts through the normal source and target boundaries."""

import json
from dataclasses import replace

import pytest
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port
from test_lifecycle_v2_round3_recovery import _fence, _manual_historical_target
from test_lifecycle_v2_round3_recovery import original as original

from finance_core.instance_admission import (
    acquire_production_owner,
    admit_production_instance,
    fence_production_instance,
    prepare_production_profile,
)
from finance_core.lifecycle_v2 import LedgerService, LifecycleError, RuntimeBinding
from finance_core.lifecycle_v2.contracts import cut_ref
from finance_core.lifecycle_v2.core_archive_origin_v1 import (
    CoreArchiveOriginV1,
    core_archive_origin_approval_verifier_v1,
    issue_core_archive_origin_v1,
    verify_core_archive_origin_v1,
)
from finance_core.lifecycle_v2.historical_evidence_v1 import historical_generations_sha256_v1
from finance_core.lifecycle_v2.recovery import (
    RecoveryAuthority,
    promote_manual_single_host_restore_v1,
)


@pytest.fixture
def source(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    with config:
        prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
        admit_production_instance(config, operation_id="admit", approval_ref="approved")
        with acquire_production_owner(config) as owner:
            yield LedgerService(
                owner, Port(), RuntimeBinding(config.instance_id, "g1", "user", "chat")
            )


@pytest.fixture
def authority():
    return RecoveryAuthority("test-core-archive-origin", b"o" * 32)


def test_origin_receipt_roundtrip_requires_preconfigured_source_authority(
    source, authority, tmp_path, monkeypatch
):
    path = tmp_path / "sealed.sqlite"
    cut = source.seal_snapshot(path)
    origin = issue_core_archive_origin_v1(source, cut, authority, snapshot_path=path)
    assert origin == issue_core_archive_origin_v1(source, cut, authority, snapshot_path=path)
    assert origin.source_admission_kind == "admit"
    assert origin.parent_history_sha256 == historical_generations_sha256_v1(())
    assert origin.cut_ref == cut_ref(cut)
    assert origin.source_binding == source.binding
    restored = CoreArchiveOriginV1.from_dict(json.loads(json.dumps(origin.to_dict())))
    assert restored == origin
    assert restored.seal_sha256 == origin.seal_sha256

    target_path = tmp_path / "target"
    target_path.mkdir(mode=0o700)
    with synthetic_config(
        target_path,
        monkeypatch,
        instance_id="target",
        verifier=core_archive_origin_approval_verifier_v1(authority),
    ) as target:
        verify_core_archive_origin_v1(restored, cut, target)
        with pytest.raises(LifecycleError):
            verify_core_archive_origin_v1(restored, replace(cut, snapshot_id="different"), target)
        different_binding = replace(restored.source_binding, client_id="another")
        with pytest.raises(LifecycleError):
            verify_core_archive_origin_v1(
                replace(restored, source_binding=different_binding), cut, target
            )

    unrelated_path = tmp_path / "unrelated"
    unrelated_path.mkdir(mode=0o700)
    with synthetic_config(unrelated_path, monkeypatch, instance_id="unrelated") as untrusted:
        with pytest.raises(LifecycleError):
            verify_core_archive_origin_v1(restored, cut, untrusted)


def test_origin_observes_current_event_watermark(source, authority, tmp_path):
    path = tmp_path / "sealed.sqlite"
    cut = source.seal_snapshot(path)
    source.capture("source1", "after-cut")
    with pytest.raises(LifecycleError):
        issue_core_archive_origin_v1(source, cut, authority, snapshot_path=path)


def test_origin_requires_exact_closed_archive(source, authority, tmp_path):
    path = tmp_path / "sealed.sqlite"
    cut = source.seal_snapshot(path)
    with pytest.raises(LifecycleError):
        issue_core_archive_origin_v1(
            source, replace(cut, database_sha256="0" * 64), authority, snapshot_path=path
        )


def test_origin_requires_current_active_admission(source, authority, tmp_path):
    path = tmp_path / "sealed.sqlite"
    cut = source.seal_snapshot(path)
    fence_production_instance(source.owner.config, operation_id="fence", approval_ref="approved")
    with pytest.raises(LifecycleError):
        issue_core_archive_origin_v1(source, cut, authority, snapshot_path=path)


def test_previous_port_only_restore_cannot_issue_a_genesis_origin(
    original, authority, tmp_path, monkeypatch
):
    old_config, owner, old_service, port = original
    path = tmp_path / "original.sqlite"
    cut = old_service.seal_snapshot(path)
    _fence(old_config, owner)
    target, held, grants = _manual_historical_target(
        tmp_path, monkeypatch, old_config, cut, path, "port-only", 1792800000
    )
    with target:
        assert (
            promote_manual_single_host_restore_v1(held, **{**grants, "port": port})["state"]
            == "ACTIVE"
        )
        with acquire_production_owner(target) as current_owner:
            current = LedgerService(
                current_owner,
                port,
                grants["binding"],
                runtime_admission=grants["runtime_admission"],
            )
            new_path = tmp_path / "port-only.sqlite"
            new_cut = current.seal_snapshot(new_path)
            with pytest.raises(LifecycleError, match="committed ancestry"):
                issue_core_archive_origin_v1(current, new_cut, authority, snapshot_path=new_path)
