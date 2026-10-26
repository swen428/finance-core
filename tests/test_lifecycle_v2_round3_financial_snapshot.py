"""Round 3 financial evidence and snapshot boundary regressions."""

import hashlib
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port, ready, response

from finance_core.instance_admission import (
    acquire_production_owner,
    admit_production_instance,
    prepare_production_profile,
)
from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.lifecycle_v2 import (
    LedgerService,
    LifecycleError,
    RuntimeBinding,
    VerifiedSource,
    snapshot_io,
)
from finance_core.profile_paths import ProfilePathError


@pytest.fixture
def live_case(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(config)
    service = LedgerService(
        owner,
        Port(),
        RuntimeBinding(config.instance_id, "gen1", "wen", "chat"),
        clock=lambda: 1792800000,
    )
    yield service, service.port, config
    if owner._lease.fd >= 0:
        owner.close()
    config.close()


def _reply(port, evidence_id, text):
    port.sources[evidence_id] = VerifiedSource(
        evidence_id,
        "wen",
        "chat",
        "message-" + evidence_id,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )


def test_replay_uses_retained_request_and_rejects_changed_reply(live_case):
    service, port, _ = live_case
    source, _, _ = ready(service)
    text = "change amount to SGD 13.00"
    _reply(port, "edit-a", text)
    port.raw = response(text, "reply", {"amount": "13.00", "currency": "SGD"})
    first = service.propose(source.source_id, "obs-edit", 1, "propose-edit", "edit-a")

    next_text = "change amount to SGD 14.00"
    _reply(port, "edit-b", next_text)
    port.raw = response(next_text, "reply", {"amount": "14.00", "currency": "SGD"})
    service.propose(source.source_id, "obs-next", 2, "propose-next", "edit-b")

    assert service.propose(source.source_id, "obs-edit", 1, "propose-edit", "edit-a") == first
    with pytest.raises(LifecycleError, match="replay mismatch"):
        service.propose(source.source_id, "obs-edit", 1, "propose-edit", "edit-b")
    with pytest.raises(LifecycleError, match="replay mismatch"):
        service.propose(source.source_id, "obs-edit", 1, "different-operation", "edit-a")


def test_correction_retry_after_later_post_returns_original_card(live_case):
    service, port, _ = live_case
    _, _, initial = ready(service)
    posted = service.confirm(initial.review_id, "action-initial", "confirm-initial")

    first_text = "change amount to SGD 15.00；原因：记录有误"
    _reply(port, "reply-first", first_text)
    port.raw = response(first_text, "reply", {"amount": "15.00", "currency": "SGD"})
    first = service.prepare_correction(
        posted.record_id, 1, "obs-first", "propose-first", "reply-first", "记录有误"
    )
    service.record_delivery(first.review_id, "delivery-first")
    first_post = service.confirm(first.review_id, "action-first", "confirm-first")

    second_text = "change amount to SGD 16.00；原因：再次修正"
    _reply(port, "reply-second", second_text)
    port.raw = response(second_text, "reply", {"amount": "16.00", "currency": "SGD"})
    second = service.prepare_correction(
        posted.record_id,
        first_post.version,
        "obs-second",
        "propose-second",
        "reply-second",
        "再次修正",
    )
    service.record_delivery(second.review_id, "delivery-second")
    service.confirm(second.review_id, "action-second", "confirm-second")
    before = service.reader().full_audit()

    replay = service.prepare_correction(
        posted.record_id, 1, "obs-first", "propose-first", "reply-first", "记录有误"
    )
    assert (replay.review_id, replay.review_sha256) == (first.review_id, first.review_sha256)
    for reply_id, reason in (("reply-second", "记录有误"), ("reply-first", "再次修正")):
        with pytest.raises(LifecycleError, match="replay mismatch"):
            service.prepare_correction(
                posted.record_id, 1, "obs-first", "propose-first", reply_id, reason
            )
    assert service.reader().full_audit() == before
    assert service.reader().get(posted.record_id)["values"]["amount"] == "16.00"


def test_retained_reply_hash_is_checked_on_point_read_audit_and_seal(live_case, tmp_path):
    service, port, _ = live_case
    _, _, review = ready(service)
    posted = service.confirm(review.review_id, "action-initial", "confirm-initial")
    text = "change amount to SGD 15.00；原因：记录有误"
    _reply(port, "correction-reply", text)
    port.raw = response(text, "reply", {"amount": "15.00", "currency": "SGD"})
    correction = service.prepare_correction(
        posted.record_id,
        posted.version,
        "obs-correction",
        "propose-correction",
        reply_evidence_id="correction-reply",
        reason="记录有误",
    )
    service.record_delivery(correction.review_id, "delivery-correction")
    service.confirm(correction.review_id, "action-correction", "confirm-correction")
    assert service.reader().get(posted.record_id)["values"]["amount"] == "15.00"
    assert service.reader().full_audit()["fact_version_count"] == 2
    service.seal_snapshot(tmp_path / "valid.sqlite")
    port.sources["correction-reply"] = replace(
        port.sources["correction-reply"], original_sha256="0" * 64
    )
    for read in (
        lambda: service.reader().get(posted.record_id),
        service.reader().full_audit,
        lambda: service.seal_snapshot(tmp_path / "rejected.sqlite"),
    ):
        with pytest.raises(LifecycleError, match="reply source"):
            read()
    rejected = tmp_path / "rejected.sqlite"
    assert rejected.exists() and stat.S_IMODE(rejected.stat().st_mode) == 0o600


def test_snapshot_is_private_from_creation_and_has_same_logical_cut(
    live_case, tmp_path, monkeypatch
):
    service, _, _ = live_case
    ready(service)
    destination = tmp_path / "cut.sqlite"
    created_modes = []
    original_init = snapshot_io._SnapshotDestination.__init__

    def inspect_creation(self, path, parent_fd, file_fd):
        created_modes.append(stat.S_IMODE(os.fstat(file_fd).st_mode))
        original_init(self, path, parent_fd, file_fd)

    monkeypatch.setattr(snapshot_io._SnapshotDestination, "__init__", inspect_creation)
    original_umask = os.umask(0)
    try:
        cut = service.seal_snapshot(destination)
    finally:
        os.umask(original_umask)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert created_modes == [0o600]
    assert cut.database_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert cut.source_binding_sha256 == service.owner.config.binding_sha256
    copy = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
    try:
        assert _logical_database_digest(copy) == cut.logical_database_sha256
    finally:
        copy.close()


def test_snapshot_refuses_existing_names_links_and_unsafe_parent(live_case, tmp_path):
    service, _, _ = live_case
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"preserve")
    linked = tmp_path / "linked.sqlite"
    os.link(existing, linked)
    dangling = tmp_path / "dangling.sqlite"
    dangling.symlink_to(tmp_path / "absent.sqlite")
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "parent-link"
    alias.symlink_to(private, target_is_directory=True)
    for destination in (
        existing,
        linked,
        dangling,
        unsafe / "new.sqlite",
        alias / "new.sqlite",
        Path("relative.sqlite"),
    ):
        with pytest.raises(LifecycleError):
            service.seal_snapshot(destination)
    assert existing.read_bytes() == b"preserve"
    assert linked.read_bytes() == b"preserve"
    assert dangling.is_symlink()
    assert not (unsafe / "new.sqlite").exists()
    assert not (private / "new.sqlite").exists()


def test_snapshot_rejects_file_acl_check_failure_and_keeps_private_partial(
    live_case, tmp_path, monkeypatch
):
    service, _, _ = live_case
    destination = tmp_path / "acl-rejected.sqlite"
    original_acl_check = snapshot_io._reject_acl_grants

    def reject_file(fd, path):
        if path == destination:
            raise ProfilePathError("synthetic file ACL grant")
        original_acl_check(fd, path)

    monkeypatch.setattr(snapshot_io, "_reject_acl_grants", reject_file)
    with pytest.raises(LifecycleError, match="cannot be verified"):
        service.seal_snapshot(destination)
    assert destination.exists() and stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_snapshot_rejects_parent_permission_change_during_copy(live_case, tmp_path, monkeypatch):
    service, _, _ = live_case
    snapshot_parent = tmp_path / "snapshot-parent"
    snapshot_parent.mkdir(mode=0o700)
    destination = snapshot_parent / "permission-changed.sqlite"
    original_backup = snapshot_io._SnapshotDestination.backup

    def change_parent_after_copy(self, source):
        original_backup(self, source)
        snapshot_parent.chmod(0o755)

    monkeypatch.setattr(snapshot_io._SnapshotDestination, "backup", change_parent_after_copy)
    try:
        with pytest.raises(LifecycleError, match="private role changed"):
            service.seal_snapshot(destination)
    finally:
        snapshot_parent.chmod(0o700)
    assert destination.exists() and stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_snapshot_rejects_named_inode_replacement_and_preserves_private_partial(
    live_case, tmp_path, monkeypatch
):
    service, _, _ = live_case
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(b"different file")
    replacement.chmod(0o600)
    moved = tmp_path / "original-partial.sqlite"
    original_backup = snapshot_io._SnapshotDestination.backup

    def replace_after_copy(self, source):
        original_backup(self, source)
        os.replace(self.path, moved)
        os.replace(replacement, self.path)

    monkeypatch.setattr(snapshot_io._SnapshotDestination, "backup", replace_after_copy)
    destination = tmp_path / "replaced.sqlite"
    with pytest.raises(LifecycleError, match="identity"):
        service.seal_snapshot(destination)
    assert destination.read_bytes() == b"different file"
    assert stat.S_IMODE(moved.stat().st_mode) == 0o600


def test_snapshot_rejects_sidecar_after_copy(live_case, tmp_path, monkeypatch):
    service, _, _ = live_case
    original_backup = snapshot_io._SnapshotDestination.backup

    def add_sidecar(self, source):
        original_backup(self, source)
        (tmp_path / "sidecar.sqlite-wal").write_bytes(b"unexpected")

    monkeypatch.setattr(snapshot_io._SnapshotDestination, "backup", add_sidecar)
    destination = tmp_path / "sidecar.sqlite"
    with pytest.raises(LifecycleError, match="sidecar"):
        service.seal_snapshot(destination)
    assert destination.exists() and stat.S_IMODE(destination.stat().st_mode) == 0o600
