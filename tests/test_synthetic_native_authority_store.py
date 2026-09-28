"""Synthetic store proofs from visible files and independently observed effects."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from finance_core.synthetic_native_authority.format import DEFAULT_LIMITS
from finance_core.synthetic_native_authority.store import (
    AuthorityError,
    CapacityError,
    FreshTestGrant,
    PoisonedError,
    QuarantinedError,
    SyntheticAuthorityStore,
    UnsupportedPlatformError,
    VerifiedRestoreTestGrant,
)
from finance_core.synthetic_native_authority.store import (
    TestAnchor as SyntheticTestAnchor,
)

HELPERS = Path(__file__).resolve().parent / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from synthetic_native_authority_oracle import (  # noqa: E402
    CapacityLimits,
    ReferenceMachine,
    allocation_fits,
    bounded_read,
    canonical_digest,
    describe_tree,
    encode_independent_frame,
    observe_entry,
    pair_capacity,
)
from synthetic_native_authority_worker import InMemoryIssuer, prepare_root  # noqa: E402

WORKER = Path(__file__).resolve().parent / "helpers" / "synthetic_native_authority_worker.py"


def _new_root() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-store-")
    container = Path(temporary.name)
    container.chmod(0o700)
    parent = container / "advertised-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    root = prepare_root(parent / "profile")
    return temporary, root


def _fresh(
    root: Path,
    *,
    limits: dict[str, int] | None = None,
    hook=None,
) -> tuple[SyntheticAuthorityStore, InMemoryIssuer]:
    issuer = InMemoryIssuer()
    store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer, limits=limits, hook=hook)
    return store, issuer


class EnrollmentFaultIssuer(InMemoryIssuer):
    """Fake enrollment port with bounded prepare/add/readback failures."""

    def __init__(self, failure: str = "none") -> None:
        super().__init__()
        self.failure = failure
        self.prepare_calls = 0
        self.add_calls = 0
        self.readback_calls = 0
        self.external_anchor: SyntheticTestAnchor | None = None
        self.external_key: bytes | None = None

    def prepare_fresh(self, root: Path) -> FreshTestGrant:
        self.prepare_calls += 1
        if self.failure == "cancel":
            raise RuntimeError("synthetic issuer cancelled")
        if self.failure == "unavailable":
            raise TimeoutError("synthetic issuer unavailable")
        return super().prepare_fresh(root)

    def add_only(self, anchor: SyntheticTestAnchor, key: bytes) -> None:
        self.add_calls += 1
        self.external_anchor = anchor
        self.external_key = key
        if self.failure == "add-conflict":
            raise RuntimeError("synthetic add-only conflict")
        super().add_only(anchor, key)

    def readback(self, enrollment_id: str) -> tuple[SyntheticTestAnchor, bytes]:
        self.readback_calls += 1
        if self.failure == "readback-unavailable":
            raise TimeoutError("synthetic issuer readback unavailable")
        anchor, key = super().readback(enrollment_id)
        if self.failure == "readback-mismatch":
            return anchor, bytes(reversed(key))
        return anchor, key


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        pytest.param("cancel", RuntimeError, id="P6-FRESH-001-cancel"),
        pytest.param("unavailable", TimeoutError, id="P6-FRESH-002-unavailable"),
    ],
)
def test_p6_fresh_prepare_failure_has_no_target_effect(
    failure: str, error_type: type[BaseException]
) -> None:
    temporary, root = _new_root()
    try:
        issuer = EnrollmentFaultIssuer(failure)
        before = describe_tree(root)
        with pytest.raises(error_type):
            SyntheticAuthorityStore.enroll_fresh_test(root, issuer)
        assert issuer.prepare_calls == 1
        assert issuer.add_calls == 0
        assert issuer.readback_calls == 0
        assert issuer._item is None
        assert describe_tree(root) == before
    finally:
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("failure", "error_type", "item_exists", "readback_calls"),
    [
        pytest.param("add-conflict", RuntimeError, False, 0, id="P6-FRESH-003-add-conflict"),
        pytest.param(
            "readback-unavailable", TimeoutError, True, 1, id="P6-FRESH-004-readback-unavailable"
        ),
        pytest.param(
            "readback-mismatch", QuarantinedError, True, 1, id="P6-FRESH-005-readback-mismatch"
        ),
    ],
)
def test_p6_fresh_external_failure_preserves_unenrolled_bytes(
    failure: str,
    error_type: type[BaseException],
    item_exists: bool,
    readback_calls: int,
) -> None:
    temporary, root = _new_root()
    try:
        issuer = EnrollmentFaultIssuer(failure)
        with pytest.raises(error_type):
            SyntheticAuthorityStore.enroll_fresh_test(root, issuer)

        assert issuer.prepare_calls == 1
        assert issuer.add_calls == 1
        assert issuer.readback_calls == readback_calls
        assert (issuer._item is not None) is item_exists
        assert issuer.external_anchor is not None and issuer.external_key is not None
        receipt = observe_entry(root / "authority" / "enrollment.receipt")
        assert receipt.data == b""
        assert len(bounded_read(root / "authority" / "registry.log")) == 4096
        assert len(bounded_read(root / "authority" / "committed-head.log")) == 2048
        before_reopen = describe_tree(root)

        # Ordinary reopen never creates or repairs a missing external item or receipt.
        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(
                root, anchor=issuer.external_anchor, key=issuer.external_key
            )
        assert issuer.add_calls == 1
        assert issuer.readback_calls == readback_calls
        assert describe_tree(root) == before_reopen
    finally:
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p6_fresh_006_nonempty_authority_collision_refuses_before_add_or_readback() -> None:
    temporary, root = _new_root()
    try:
        collision = root / "authority"
        collision.mkdir(mode=0o700)
        collision.chmod(0o700)
        sentinel = collision / "preexisting.synthetic"
        sentinel.write_bytes(b"PREEXISTING_TARGET_SENTINEL")
        sentinel.chmod(0o600)
        before = describe_tree(root)
        issuer = EnrollmentFaultIssuer()

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.enroll_fresh_test(root, issuer)

        assert issuer.prepare_calls == 1
        assert issuer.add_calls == 0
        assert issuer.readback_calls == 0
        assert describe_tree(root) == before
    finally:
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p1_cold_001_subprocess_replays_both_kinds_from_disk_only() -> None:
    temporary, root = _new_root()
    result_read_fd, result_write_fd = os.pipe()
    try:
        seeded = subprocess.run(
            [
                sys.executable,
                str(WORKER),
                "seed",
                "--root",
                str(root),
                "--result-fd",
                str(result_write_fd),
            ],
            pass_fds=(result_write_fd,),
            capture_output=True,
            timeout=30,
            check=False,
        )
        os.close(result_write_fd)
        result_write_fd = -1
        with os.fdopen(result_read_fd, "rb") as private_result:
            result_read_fd = -1
            private_bytes = private_result.read(32_769)
        assert seeded.returncode == 0, seeded.stderr.decode("utf-8", errors="replace")
        assert seeded.stdout == b""
        assert 0 < len(private_bytes) <= 32_768
        grant = json.loads(private_bytes.decode("ascii"))
        anchor = grant["anchor"]
        key = base64.b64decode(grant["key"], validate=True)
        assert len(key) == 32

        before = describe_tree(root)
        request = json.dumps(
            {"anchor": anchor, "key": base64.b64encode(key).decode("ascii")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        reopened = subprocess.run(
            [sys.executable, str(WORKER), "reopen", "--root", str(root)],
            input=request,
            capture_output=True,
            timeout=30,
            check=False,
        )
        after = describe_tree(root)
        assert reopened.returncode == 0, reopened.stderr.decode("utf-8", errors="replace")
        assert after == before
        observed = json.loads(reopened.stdout.decode("ascii"))

        model = ReferenceMachine()
        limits = CapacityLimits(
            registry_record_cap=DEFAULT_LIMITS["registry_record_cap"],
            head_record_cap=DEFAULT_LIMITS["head_record_cap"],
            registry_byte_cap=DEFAULT_LIMITS["registry_byte_cap"],
            head_byte_cap=DEFAULT_LIMITS["head_byte_cap"],
        )
        for kind in ("JOURNAL", "WAL"):
            generation, slot = model.allocate(kind, limits)
            model.retire(kind)
            model.release((kind, generation, slot))
            model.allocate(kind, limits)

        registry = bounded_read(root / "authority" / "registry.log")
        head = bounded_read(root / "authority" / "committed-head.log")
        assert len(registry) == 15 * 4096
        assert len(head) == 15 * 2048
        assert observed["seq"] == model.seq == 15
        assert observed["state"] == model.snapshot_state()
        assert observed["component_state"] == "ENROLLED_TEST_ONLY"
        assert observed["head_digest"] == hashlib.sha256(head[-2048:]).hexdigest()
    finally:
        if result_write_fd >= 0:
            os.close(result_write_fd)
        if result_read_fd >= 0:
            os.close(result_read_fd)
        temporary.cleanup()


def _active_store(
    root: Path,
    *,
    kind: str = "JOURNAL",
    limits: dict[str, int] | None = None,
) -> tuple[SyntheticAuthorityStore, InMemoryIssuer, object]:
    store, issuer = _fresh(root, limits=limits)
    token = store.allocate(kind, op_id=f"{1:032x}")
    store.write(token, b"SYNTHETIC_ACTIVE_SENTINEL")
    return store, issuer, token


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p5_model_001_reference_machine_matches_retained_slot_selector_history() -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, _issuer = _fresh(root)
        limits = CapacityLimits(
            registry_record_cap=DEFAULT_LIMITS["registry_record_cap"],
            head_record_cap=DEFAULT_LIMITS["head_record_cap"],
            registry_byte_cap=DEFAULT_LIMITS["registry_byte_cap"],
            head_byte_cap=DEFAULT_LIMITS["head_byte_cap"],
        )
        model = ReferenceMachine()
        assert model.matches(store.snapshot())

        first_generation, first_slot = model.allocate("JOURNAL", limits)
        first = store.allocate("JOURNAL", op_id=f"{101:032x}")
        assert (first_generation, first_slot) == (1, 0)
        assert model.matches(store.snapshot())
        held = store.open_token("JOURNAL")
        model.open_active("JOURNAL")

        store.retire(first, op_id=f"{102:032x}")
        model.retire("JOURNAL")
        assert model.matches(store.snapshot())
        before_stale = describe_tree(root)
        with pytest.raises(AuthorityError):
            store.write(first, b"STALE_MUST_NOT_WRITE")
        with pytest.raises(AuthorityError):
            store.retire(first, op_id=f"{103:032x}")
        assert describe_tree(root) == before_stale
        assert model.matches(store.snapshot())

        second_generation, second_slot = model.allocate("JOURNAL", limits)
        second = store.allocate("JOURNAL", op_id=f"{104:032x}")
        assert (second_generation, second_slot) == (2, 1)
        assert model.matches(store.snapshot())
        store.retire(second, op_id=f"{105:032x}")
        model.retire("JOURNAL")
        store.close_token(second)
        model.release(("JOURNAL", second_generation, second_slot))

        third_generation, third_slot = model.allocate("JOURNAL", limits)
        third = store.allocate("JOURNAL", op_id=f"{106:032x}")
        assert (third_generation, third_slot) == (3, 1)
        assert model.matches(store.snapshot())
        store.retire(third, op_id=f"{107:032x}")
        model.retire("JOURNAL")
        store.close_token(third)
        model.release(("JOURNAL", third_generation, third_slot))

        store.close_token(first)
        model.release(("JOURNAL", first_generation, first_slot))
        store.close_token(held)
        model.release(("JOURNAL", first_generation, first_slot))
        fourth_generation, fourth_slot = model.allocate("JOURNAL", limits)
        fourth = store.allocate("JOURNAL", op_id=f"{108:032x}")
        assert (fourth_generation, fourth_slot) == (4, 0)
        assert model.matches(store.snapshot())
        store.retire(fourth, op_id=f"{109:032x}")
        model.retire("JOURNAL")
        store.close_token(fourth)
        model.release(("JOURNAL", fourth_generation, fourth_slot))
        assert model.matches(store.snapshot())
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p1_cold_002_rejects_byte_identical_tree_at_new_root_inodes() -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("WAL", op_id=f"{120:032x}")
        store.write(token, b"COPIED_ROOT_SENTINEL")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close_token(token)
        store.close()
        store = None

        copied_parent = root.parent.parent / "copied-parent"
        copied_parent.mkdir(mode=0o700)
        copied_parent.chmod(0o700)
        copied_root = copied_parent / root.name
        shutil.copytree(root, copied_root, copy_function=shutil.copy2)
        source_tree = describe_tree(root)
        copied_before = describe_tree(copied_root)
        assert source_tree.keys() == copied_before.keys()
        for relative_path, original in source_tree.items():
            copied = copied_before[relative_path]
            assert copied.data == original.data
            assert copied.kind == original.kind
            assert (copied.device, copied.inode) != (original.device, original.inode)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(copied_root, anchor=anchor, key=key)

        assert describe_tree(copied_root) == copied_before
        assert describe_tree(root) == source_tree
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    [
        pytest.param("authority/committed-head.log", "extra", id="P4-REPLAY-001-extra-H-byte"),
        pytest.param(
            "authority/committed-head.log", "truncate", id="P4-REPLAY-002-partial-H-frame"
        ),
        pytest.param("authority/registry.log", "mac", id="P4-REPLAY-003-bad-R-mac"),
    ],
)
def test_cold_replay_quarantines_bad_log_bytes_without_rewrite(
    relative_path: str, mutation: str
) -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("JOURNAL", op_id=f"{130:032x}")
        store.write(token, b"REPLAY_SENTINEL")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close_token(token)
        store.close()
        store = None

        path = root / relative_path
        fd = os.open(path, os.O_RDWR)
        try:
            info = os.fstat(fd)
            if mutation == "extra":
                assert os.pwrite(fd, b"X", info.st_size) == 1
            elif mutation == "truncate":
                os.ftruncate(fd, info.st_size - 1)
            else:
                last = os.pread(fd, 1, info.st_size - 1)
                assert len(last) == 1
                assert os.pwrite(fd, bytes([last[0] ^ 1]), info.st_size - 1) == 1
            os.fsync(fd)
        finally:
            os.close(fd)
        before = describe_tree(root)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)

        assert describe_tree(root) == before
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param("P4-REPLAY-004-authenticated-missing-field", id="P4-REPLAY-004-missing-field"),
        pytest.param("P4-REPLAY-005-authenticated-pair-mismatch", id="P4-REPLAY-005-pair-mismatch"),
        pytest.param(
            "P4-REPLAY-006-authenticated-illegal-transition", id="P4-REPLAY-006-transition"
        ),
        pytest.param("P4-REPLAY-007-authenticated-state-digest", id="P4-REPLAY-007-state-digest"),
        pytest.param("P4-REPLAY-008-authenticated-sequence-gap", id="P4-REPLAY-008-sequence-gap"),
        pytest.param("P4-REPLAY-009-duplicate-event-id", id="P4-REPLAY-009-event-id"),
        pytest.param("P4-REPLAY-011-generation-mismatch", id="P4-REPLAY-011-generation"),
        pytest.param("P4-REPLAY-012-op-id-reuse", id="P4-REPLAY-012-op-id"),
        pytest.param("P4-REPLAY-013-slot-mismatch", id="P4-REPLAY-013-slot"),
        pytest.param("P4-REPLAY-014-phase-order", id="P4-REPLAY-014-phase"),
        pytest.param("P4-REPLAY-015-epoch-mismatch", id="P4-REPLAY-015-epoch"),
        pytest.param("P4-REPLAY-016-invalid-enum", id="P4-REPLAY-016-enum"),
        pytest.param("P4-REPLAY-017-generation-range", id="P4-REPLAY-017-range"),
        pytest.param("P4-REPLAY-018-extra-field", id="P4-REPLAY-018-extra-field"),
        pytest.param("P4-REPLAY-019-invalid-id", id="P4-REPLAY-019-invalid-id"),
        pytest.param("P4-REPLAY-020-duplicate-key", id="P4-REPLAY-020-duplicate-key"),
    ],
)
def test_authenticated_invalid_history_is_quarantined_without_rewrite(case_id: str) -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("JOURNAL", op_id=f"{131:032x}")
        store.write(token, b"AUTHENTICATED_REPLAY_SENTINEL")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close_token(token)
        store.close()
        store = None

        registry_path = root / "authority" / "registry.log"
        head_path = root / "authority" / "committed-head.log"
        registry = bounded_read(registry_path)
        head = bounded_read(head_path)
        registry_frame = registry[-4096:]
        head_frame = head[-2048:]

        def payload(frame: bytes) -> dict[str, object]:
            length = int.from_bytes(frame[8:12], "big")
            value = json.loads(frame[12 : 12 + length].decode("ascii"))
            assert isinstance(value, dict)
            return value

        record = payload(registry_frame)
        committed = payload(head_frame)
        raw_registry: bytes | None = None
        if case_id == "P4-REPLAY-004-authenticated-missing-field":
            record.pop("event_id")
        elif case_id == "P4-REPLAY-005-authenticated-pair-mismatch":
            committed["event_id"] = "f" * 32
        elif case_id == "P4-REPLAY-006-authenticated-illegal-transition":
            record["event"] = "RETIRED"
        elif case_id == "P4-REPLAY-007-authenticated-state-digest":
            record["result_state_digest"] = "f" * 64
            committed["state_digest"] = "f" * 64
        elif case_id == "P4-REPLAY-008-authenticated-sequence-gap":
            record["seq"] = int(record["seq"]) + 5
            committed["seq"] = record["seq"]
        elif case_id == "P4-REPLAY-009-duplicate-event-id":
            previous_record = payload(registry[-8192:-4096])
            record["event_id"] = previous_record["event_id"]
            committed["event_id"] = previous_record["event_id"]
        elif case_id == "P4-REPLAY-011-generation-mismatch":
            record["generation"] = int(record["generation"]) + 1
        elif case_id == "P4-REPLAY-012-op-id-reuse":
            genesis = payload(registry[:4096])
            record["op_id"] = genesis["op_id"]
            committed["op_id"] = genesis["op_id"]
        elif case_id == "P4-REPLAY-013-slot-mismatch":
            record["slot"] = 1 - int(record["slot"])
        elif case_id == "P4-REPLAY-014-phase-order":
            record["event"] = "RESET_DONE"
        elif case_id == "P4-REPLAY-015-epoch-mismatch":
            record["issuer_epoch"] = "e" * 32
            committed["issuer_epoch"] = record["issuer_epoch"]
        elif case_id == "P4-REPLAY-016-invalid-enum":
            record["event"] = "NOT_AN_EVENT"
        elif case_id == "P4-REPLAY-017-generation-range":
            record["generation"] = 64
        elif case_id == "P4-REPLAY-018-extra-field":
            record["unexpected"] = "synthetic-test-field"
        elif case_id == "P4-REPLAY-019-invalid-id":
            record["profile_id"] = "A" * 32
            committed["profile_id"] = record["profile_id"]
        else:
            canonical = json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            raw_registry = (
                canonical[:-1] + b',"event_id":"' + str(record["event_id"]).encode("ascii") + b'"}'
            )

        new_registry_frame = encode_independent_frame("registry", record, key, raw=raw_registry)
        committed["record_digest"] = hashlib.sha256(new_registry_frame).hexdigest()
        new_head_frame = encode_independent_frame("head", committed, key)

        def overwrite_frame(path: Path, offset: int, frame: bytes) -> None:
            fd = os.open(path, os.O_RDWR)
            try:
                cursor = 0
                while cursor < len(frame):
                    count = os.pwrite(fd, frame[cursor:], offset + cursor)
                    if count <= 0:
                        raise OSError("test frame rewrite made no progress")
                    cursor += count
                os.fsync(fd)
            finally:
                os.close(fd)

        overwrite_frame(registry_path, len(registry) - 4096, new_registry_frame)
        overwrite_frame(head_path, len(head) - 2048, new_head_frame)
        before = describe_tree(root)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)

        assert describe_tree(root) == before
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p4_replay_010_missing_head_is_quarantined_without_recreation() -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("WAL", op_id=f"{132:032x}")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close_token(token)
        store.close()
        store = None

        head_path = root / "authority" / "committed-head.log"
        held_path = root.parent / f"held-head-{uuid.uuid4().hex}"
        original_head = bounded_read(head_path)
        head_path.rename(held_path)
        before = describe_tree(root)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)

        assert describe_tree(root) == before
        assert not head_path.exists()
        assert bounded_read(held_path) == original_head
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("role", "frame_size"),
    [
        pytest.param("registry", 4096, id="P4-REPLAY-021-registry-single-sided-rollback"),
        pytest.param("head", 2048, id="P4-REPLAY-022-head-single-sided-rollback"),
    ],
)
def test_single_sided_log_rollback_quarantines_without_repair(role: str, frame_size: int) -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("JOURNAL", op_id=f"{133:032x}")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close_token(token)
        store.close()
        store = None

        path = root / "authority" / ("registry.log" if role == "registry" else "committed-head.log")
        fd = os.open(path, os.O_RDWR)
        try:
            assert os.fstat(fd).st_size == frame_size * 4
            os.ftruncate(fd, frame_size)
            os.fsync(fd)
        finally:
            os.close(fd)
        before = describe_tree(root)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)

        assert describe_tree(root) == before
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p4_rollback_001_coherent_genesis_prefix_is_not_detected_as_freshness() -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("JOURNAL", op_id=f"{134:032x}")
        store.close_token(token)
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close()
        store = None

        registry_path = root / "authority" / "registry.log"
        head_path = root / "authority" / "committed-head.log"
        old_registry_prefix = bounded_read(registry_path)[:4096]
        old_head_prefix = bounded_read(head_path)[:2048]
        for path, prefix in (
            (registry_path, old_registry_prefix),
            (head_path, old_head_prefix),
        ):
            fd = os.open(path, os.O_RDWR)
            try:
                assert os.pwrite(fd, prefix, 0) == len(prefix)
                os.ftruncate(fd, len(prefix))
                os.fsync(fd)
            finally:
                os.close(fd)
        before = describe_tree(root)

        reopened = SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
        try:
            model = ReferenceMachine()
            snapshot = reopened.snapshot()
            assert model.matches(snapshot)
            assert snapshot.seq == 1
            assert snapshot.head_digest == hashlib.sha256(old_head_prefix).hexdigest()
        finally:
            reopened.close()
        assert describe_tree(root) == before
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p4_replay_023_logs_from_a_different_epoch_are_quarantined_unchanged() -> None:
    temporary, first_root = _new_root()
    first_store: SyntheticAuthorityStore | None = None
    second_store: SyntheticAuthorityStore | None = None
    try:
        second_parent = first_root.parent.parent / "second-parent"
        second_parent.mkdir(mode=0o700)
        second_parent.chmod(0o700)
        second_root = prepare_root(second_parent / "profile")
        first_store, first_issuer = _fresh(first_root)
        second_store, _second_issuer = _fresh(second_root)
        assert first_issuer.grant is not None
        anchor, key = first_issuer.readback(first_issuer.grant.enrollment_id)
        first_store.close()
        first_store = None
        second_store.close()
        second_store = None

        for name in ("registry.log", "committed-head.log"):
            source = bounded_read(second_root / "authority" / name)
            target = first_root / "authority" / name
            fd = os.open(target, os.O_RDWR)
            try:
                assert os.fstat(fd).st_size == len(source)
                assert os.pwrite(fd, source, 0) == len(source)
                os.fsync(fd)
            finally:
                os.close(fd)
        first_before = describe_tree(first_root)
        second_before = describe_tree(second_root)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(first_root, anchor=anchor, key=key)

        assert describe_tree(first_root) == first_before
        assert describe_tree(second_root) == second_before
    finally:
        for open_store in (first_store, second_store):
            if open_store is not None:
                try:
                    open_store.close()
                except PoisonedError:
                    pass
        temporary.cleanup()


class ArchivedTestIssuer(InMemoryIssuer):
    """Fake restore authority backed by copied, historical synthetic bytes."""

    def __init__(
        self,
        *,
        source_anchor,
        source_key: bytes,
        source_material: dict[str, bytes],
        cut_frame: bytes,
        cut_key: bytes,
        cut_id: str,
        closure_digest: str,
        sentinel_digest: str,
        fresh_overrides: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.source_anchor = source_anchor
        self.source_key = source_key
        self.source_material = source_material
        self.cut_frame = cut_frame
        self.cut_key = cut_key
        self.cut_id = cut_id
        self.closure_digest = closure_digest
        self.sentinel_digest = sentinel_digest
        self.fresh_overrides = fresh_overrides or {}
        self.restore_prepared = False

    def prepare_verified_restore(self, root: Path) -> VerifiedRestoreTestGrant:
        # The external fake supplies a new target grant and old archive evidence;
        # Core receives no old live path or old descriptor-inode authority.
        self.restore_prepared = True
        fresh = self.prepare_fresh(root)
        if self.fresh_overrides:
            fresh = replace(fresh, **self.fresh_overrides)
        return VerifiedRestoreTestGrant(
            fresh=fresh,
            source_anchor=self.source_anchor,
            source_key=self.source_key,
            source_descriptor=self.source_material["descriptor"],
            source_registry=self.source_material["registry"],
            source_head=self.source_material["head"],
            source_receipt=self.source_material["receipt"],
            cut_frame=self.cut_frame,
            cut_key=self.cut_key,
            expected_cut_id=self.cut_id,
            expected_closed_owner_inventory_digest=self.closure_digest,
            expected_snapshot_sentinel_digest=self.sentinel_digest,
        )


def _write_private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("test file write made no progress")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_source(
    temporary_root: Path, *, pending: bool = False
) -> tuple[Path, Path, ArchivedTestIssuer, dict[str, object]]:
    source_parent = temporary_root / "source-parent"
    source_parent.mkdir(mode=0o700)
    source_parent.chmod(0o700)
    source_root = prepare_root(source_parent / "profile")
    armed = [False]

    def archive_hook(event: str, evidence: dict[str, object]) -> None:
        if (
            pending
            and armed[0]
            and event == "before_publish"
            and evidence.get("phase") == "RESET_INTENT"
        ):
            raise RuntimeError("leave a complete pending synthetic INTENT for replay testing")

    source_issuer = InMemoryIssuer()
    source_store = SyntheticAuthorityStore.enroll_fresh_test(
        source_root, source_issuer, hook=archive_hook
    )
    assert source_issuer.grant is not None
    source_snapshot = None
    if pending:
        armed[0] = True
        with pytest.raises(RuntimeError, match="complete pending synthetic INTENT"):
            source_store.allocate("WAL", op_id=f"{50:032x}")
    else:
        source_token = source_store.allocate("WAL", op_id=f"{50:032x}")
        source_store.write(source_token, b"SYNTHETIC_ARCHIVE_SENTINEL")
        source_store.close_token(source_token)
        source_snapshot = source_store.snapshot()
    assert source_issuer.grant is not None
    source_anchor, source_key = source_issuer.readback(source_issuer.grant.enrollment_id)
    try:
        source_store.close()
    except PoisonedError:
        if not pending:
            raise

    archive_container = temporary_root / "archive-copy"
    archive_container.mkdir(mode=0o700)
    archive_container.chmod(0o700)
    archive_dir = archive_container / "native-authority"
    archive_dir.mkdir(mode=0o700)
    archive_dir.chmod(0o700)
    source_paths = {
        "descriptor": source_root / "authority" / "capabilities.frame",
        "registry": source_root / "authority" / "registry.log",
        "head": source_root / "authority" / "committed-head.log",
        "receipt": source_root / "authority" / "enrollment.receipt",
    }
    archive_names = {
        "descriptor": "capabilities.frame",
        "registry": "registry.log",
        "head": "committed-head.log",
        "receipt": "enrollment.receipt",
    }
    copied: dict[str, bytes] = {}
    for role, path in source_paths.items():
        contents = bounded_read(path)
        archive_path = archive_dir / archive_names[role]
        _write_private_file(archive_path, contents)
        before = observe_entry(path)
        after = observe_entry(archive_path)
        assert after.data == before.data
        assert (after.device, after.inode) != (before.device, before.inode)
        copied[role] = bounded_read(archive_path)

    cut_id = uuid.uuid4().hex
    # These fake-port declarations exercise cut-field binding only; the test
    # does not derive them from a real owner inventory or sentinel artifact.
    closure_digest = hashlib.sha256(b"synthetic-closed-owner-inventory").hexdigest()
    sentinel_digest = hashlib.sha256(b"synthetic-archive-sentinel").hexdigest()
    cut_key = os.urandom(32)
    cut_payload = {
        "v": 1,
        "purpose": "synthetic-cut-only",
        "source_profile_id": source_anchor.profile_id,
        "source_instance_id": source_anchor.instance_id,
        "source_issuer_epoch": source_anchor.issuer_epoch,
        "source_registry_id": source_anchor.registry_id,
        "source_key_id": source_anchor.key_id,
        "cut_id": cut_id,
        "c_digest": source_anchor.c_digest,
        "registry_file_digest": hashlib.sha256(copied["registry"]).hexdigest(),
        "head_file_digest": hashlib.sha256(copied["head"]).hexdigest(),
        "accepted_seq": (
            source_snapshot.seq if source_snapshot is not None else len(copied["registry"]) // 4096
        ),
        "accepted_head_digest": (
            source_snapshot.head_digest
            if source_snapshot is not None
            else hashlib.sha256(copied["head"][-2048:]).hexdigest()
        ),
        "state_digest": (
            canonical_digest(source_snapshot.state)
            if source_snapshot is not None
            else canonical_digest({"JOURNAL": {}, "WAL": {}})
        ),
        "closed_owner_inventory_digest": closure_digest,
        "snapshot_sentinel_digest": sentinel_digest,
    }
    cut_frame = encode_independent_frame("test-cut", cut_payload, cut_key)
    issuer = ArchivedTestIssuer(
        source_anchor=source_anchor,
        source_key=source_key,
        source_material=copied,
        cut_frame=cut_frame,
        cut_key=cut_key,
        cut_id=cut_id,
        closure_digest=closure_digest,
        sentinel_digest=sentinel_digest,
    )
    archive_before = describe_tree(archive_dir)
    return (
        source_root,
        archive_dir,
        issuer,
        {
            "copied": copied,
            "cut_payload": cut_payload,
            "cut_key": cut_key,
            "source_anchor": source_anchor,
            "source_key": source_key,
            "archive_before": archive_before,
            "source_snapshot": source_snapshot,
            "pending": pending,
            "target_parent": temporary_root / "target-parent",
        },
    )


def _operation(store: SyntheticAuthorityStore, token: object, name: str) -> object:
    if name == "read":
        return store.read(token)
    if name == "write":
        return store.write(token, b"MUST_NOT_BE_WRITTEN")
    if name == "sync":
        return store.sync(token)
    if name == "query":
        return store.query()
    if name == "open":
        return store.open_token("JOURNAL")
    if name == "allocate":
        return store.allocate("WAL", op_id=f"{2:032x}")
    if name == "retire":
        return store.retire(token, op_id=f"{3:032x}")
    raise AssertionError(f"unknown test operation: {name}")


def _replace_file(path: Path, root: Path, foreign: bytes) -> tuple[Path, bytes]:
    original = observe_entry(path)
    assert original.kind == "regular" and original.data is not None
    held = root.parent / f"held-{uuid.uuid4().hex}"
    path.rename(held)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, foreign)
        os.fsync(fd)
    finally:
        os.close(fd)
    assert bounded_read(held) == original.data
    return held, original.data


def _replace_directory(path: Path, root: Path) -> tuple[Path, dict[str, object]]:
    held = root.parent / f"held-dir-{uuid.uuid4().hex}"
    before = describe_tree(path)
    path.rename(held)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    names = {
        "authority": (
            "capabilities.frame",
            "registry.log",
            "committed-head.log",
            "enrollment.receipt",
            "core-lifecycle.lock",
        ),
        "slots": ("journal-0.slot", "journal-1.slot", "wal-0.slot", "wal-1.slot"),
    }[path.name]
    for name in names:
        target = path / name
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, f"FOREIGN:{path.name}:{name}".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return held, before


def _refusal_effects_unchanged(
    store: SyntheticAuthorityStore,
    root: Path,
    operation: str,
    token: object,
    *,
    held_file: Path | None = None,
    held_tree: Path | None = None,
    old_tree: dict[str, object] | None = None,
) -> None:
    target_before = describe_tree(root)
    held_before = observe_entry(held_file) if held_file is not None else None
    held_tree_before = describe_tree(held_tree) if held_tree is not None else None
    error: BaseException | None = None
    result: object | None = None
    try:
        result = _operation(store, token, operation)
    except BaseException as exc:
        error = exc
    target_after = describe_tree(root)

    assert isinstance(error, PoisonedError), (
        f"operation unexpectedly returned {result!r} or raised {error!r}; "
        f"entry tree before={target_before!r}; after={target_after!r}; "
        f"old tree before replacement={old_tree!r}"
    )
    assert target_after == target_before
    if held_file is not None:
        assert observe_entry(held_file) == held_before
    if held_tree is not None:
        assert describe_tree(held_tree) == held_tree_before
    with pytest.raises(PoisonedError):
        store.query()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("case_id", "relative_path", "operation"),
    [
        pytest.param(
            "P1-V2-001-main-existing-write", "main.witness", "write", id="P1-V2-001-main-write"
        ),
        pytest.param("P1-V2-002-main-new-open", "main.witness", "open", id="P1-V2-002-main-open"),
        pytest.param(
            "P1-V2-003-registry-existing-write",
            "authority/registry.log",
            "write",
            id="P1-V2-003-registry-write",
        ),
        pytest.param(
            "P1-V2-004-head-followup-allocation",
            "authority/committed-head.log",
            "allocate",
            id="P1-V2-004-head-allocate",
        ),
        pytest.param(
            "P1-ACTIVE-001-descriptor-query",
            "authority/capabilities.frame",
            "query",
            id="P1-ACTIVE-001-descriptor",
        ),
        pytest.param(
            "P1-ACTIVE-002-receipt-read",
            "authority/enrollment.receipt",
            "read",
            id="P1-ACTIVE-002-receipt",
        ),
        pytest.param("P1-ACTIVE-003-slot-sync", "__active_slot__", "sync", id="P1-ACTIVE-003-slot"),
    ],
)
def test_active_file_substitution_refuses_without_original_or_foreign_effects(
    case_id: str, relative_path: str, operation: str
) -> None:
    del case_id
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, _issuer, token = _active_store(root)
        if relative_path == "__active_slot__":
            active = store.snapshot().state["JOURNAL"]["active"]
            assert active is not None
            relative_path = f"slots/journal-{active['slot']}.slot"
        path = root / relative_path
        foreign = f"FOREIGN:{relative_path}".encode("ascii")
        held, original_bytes = _replace_file(path, root, foreign)
        assert observe_entry(path).data == foreign
        assert bounded_read(held) == original_bytes

        _refusal_effects_unchanged(store, root, operation, token, held_file=held)
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("case_id", "which_directory"),
    [
        pytest.param("P1-V2-005-advertised-parent", "parent", id="P1-V2-005-parent-swap"),
        pytest.param(
            "P1-ACTIVE-004-authority-directory", "authority", id="P1-ACTIVE-004-authority-swap"
        ),
        pytest.param("P1-ACTIVE-005-slots-directory", "slots", id="P1-ACTIVE-005-slots-swap"),
    ],
)
def test_active_directory_substitution_refuses_without_tree_changes(
    case_id: str, which_directory: str
) -> None:
    del case_id
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    held: Path | None = None
    old_root_tree: dict[str, object] | None = None
    try:
        store, _issuer, token = _active_store(root)
        if which_directory in {"authority", "slots"}:
            held, old_root_tree = _replace_directory(root / which_directory, root)
            # The parked directory's full bytes and entry identities are preserved.
            _refusal_effects_unchanged(
                store, root, "write", token, held_tree=held, old_tree=old_root_tree
            )
        else:
            container = root.parent.parent
            old_parent = root.parent
            old_root_tree = describe_tree(root)
            held = container / f"held-parent-{uuid.uuid4().hex}"
            old_parent.rename(held)
            replacement_parent = container / old_parent.name
            replacement_parent.mkdir(mode=0o700)
            replacement_parent.chmod(0o700)
            replacement_root = prepare_root(replacement_parent / "profile")
            target_before = describe_tree(replacement_root)
            parked_before = describe_tree(held / "profile")
            error: BaseException | None = None
            try:
                store.write(token, b"MUST_NOT_BE_WRITTEN")
            except BaseException as exc:
                error = exc
            assert isinstance(error, PoisonedError), (
                f"parent swap operation returned success or wrong error: {error!r}"
            )
            assert describe_tree(replacement_root) == target_before
            assert describe_tree(held / "profile") == parked_before
            with pytest.raises(PoisonedError):
                store.query()
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("write", id="P1-ACTIVE-006-c-truncate-write"),
        pytest.param("sync", id="P1-ACTIVE-006-c-truncate-sync"),
        pytest.param("query", id="P1-ACTIVE-006-c-truncate-query"),
    ],
)
def test_truncated_descriptor_inode_blocks_active_use(operation: str) -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, _issuer, token = _active_store(root)
        descriptor = root / "authority" / "capabilities.frame"
        original = observe_entry(descriptor)
        assert original.kind == "regular" and original.data is not None
        fd = os.open(descriptor, os.O_WRONLY)
        try:
            os.ftruncate(fd, 0)
            os.fsync(fd)
        finally:
            os.close(fd)
        truncated = observe_entry(descriptor)
        assert truncated.device == original.device and truncated.inode == original.inode
        assert truncated.data == b""
        before = describe_tree(root)

        error: BaseException | None = None
        result: object | None = None
        try:
            result = _operation(store, token, operation)
        except BaseException as exc:
            error = exc
        after = describe_tree(root)
        assert isinstance(error, PoisonedError), (
            f"{operation} accepted an empty descriptor on its issued inode; "
            f"result={result!r}; before={before!r}; after={after!r}"
        )
        assert after == before
        assert observe_entry(descriptor).inode == original.inode
        assert (
            bounded_read(root / "authority" / "registry.log")
            == before["authority/registry.log"].data
        )
        assert (
            bounded_read(root / "authority" / "committed-head.log")
            == before["authority/committed-head.log"].data
        )
        with pytest.raises(PoisonedError):
            store.query()
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p1_active_007_wrong_actual_fd_is_rejected_after_pathname_restore() -> None:
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        store, issuer = _fresh(root)
        token = store.allocate("JOURNAL", op_id=f"{40:032x}")
        store.write(token, b"ACTIVE_MAIN_SENTINEL")
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close()
        store = None

        main = root / "main.witness"
        original = observe_entry(main)
        assert original.data is not None
        foreign_bytes = b"FOREIGN_MAIN_FD_SENTINEL"
        opened_witness: dict[str, int] = {}
        foreign_path: Path | None = None
        prepared: dict[str, Path] = {}

        def swap_after_the_actual_open(event: str, evidence: dict[str, object]) -> None:
            nonlocal foreign_path
            if event == "before_object_open" and evidence.get("role") == "main":
                held_original, _ = _replace_file(main, root, foreign_bytes)
                prepared["original"] = held_original
            elif event == "after_object_open" and evidence.get("role") == "main":
                opened_witness["dev"] = int(evidence["dev"])
                opened_witness["ino"] = int(evidence["ino"])
                foreign_path = root.parent / f"foreign-opened-{uuid.uuid4().hex}"
                main.rename(foreign_path)
                prepared["original"].rename(main)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(
                root, anchor=anchor, key=key, hook=swap_after_the_actual_open
            )

        assert foreign_path is not None
        foreign = observe_entry(foreign_path)
        restored = observe_entry(main)
        assert (opened_witness["dev"], opened_witness["ino"]) == (foreign.device, foreign.inode)
        assert (restored.device, restored.inode) == (original.device, original.inode)
        assert restored.data == original.data
        assert foreign.data == foreign_bytes
        assert describe_tree(root)["main.witness"] == original
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


def _limits_for_pair_cap(**overrides: int) -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    limits.update(
        registry_record_cap=9,
        head_record_cap=9,
        registry_byte_cap=9 * 4096,
        head_byte_cap=9 * 2048,
    )
    limits.update(overrides)
    return limits


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("limiter", "limits_override"),
    [
        pytest.param(
            "registry-records", {"registry_record_cap": 9}, id="P3-RESERVE-001-registry-count"
        ),
        pytest.param("head-records", {"head_record_cap": 9}, id="P3-RESERVE-002-head-count"),
        pytest.param(
            "registry-bytes", {"registry_byte_cap": 9 * 4096}, id="P3-RESERVE-003-registry-bytes"
        ),
        pytest.param("head-bytes", {"head_byte_cap": 9 * 2048}, id="P3-RESERVE-004-head-bytes"),
    ],
)
@pytest.mark.parametrize(
    "first_retirement",
    ["JOURNAL", "WAL"],
    ids=["P3-RESERVE-005-journal-first", "P3-RESERVE-006-wal-first"],
)
def test_dual_active_retirement_room_survives_capacity_refusal(
    limiter: str, limits_override: dict[str, int], first_retirement: str
) -> None:
    del limiter
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        limits = _limits_for_pair_cap(**limits_override)
        assert (
            pair_capacity(
                CapacityLimits(
                    limits["registry_record_cap"],
                    limits["head_record_cap"],
                    limits["registry_byte_cap"],
                    limits["head_byte_cap"],
                )
            )
            == 9
        )
        store, _issuer = _fresh(root, limits=limits)
        journal = store.allocate("JOURNAL", op_id=f"{1:032x}")
        wal = store.allocate("WAL", op_id=f"{2:032x}")
        store.write(wal, b"COMMITTED_WAL_SENTINEL")
        tokens = {"JOURNAL": journal, "WAL": wal}
        active_state = store.snapshot().state
        wal_slot = active_state["WAL"]["active"]["slot"]
        wal_path = root / "slots" / f"wal-{wal_slot}.slot"
        sentinel_before = bounded_read(wal_path)
        assert sentinel_before == b"COMMITTED_WAL_SENTINEL"

        second_retirement = "WAL" if first_retirement == "JOURNAL" else "JOURNAL"
        store.retire(tokens[first_retirement], op_id=f"{10:032x}")
        store.close_token(tokens[first_retirement])
        before_refusal = describe_tree(root)
        r_before = bounded_read(root / "authority" / "registry.log")
        h_before = bounded_read(root / "authority" / "committed-head.log")
        assert not allocation_fits(
            limits=CapacityLimits(
                limits["registry_record_cap"],
                limits["head_record_cap"],
                limits["registry_byte_cap"],
                limits["head_byte_cap"],
            ),
            committed_pairs=8,
            phases={second_retirement: "ACTIVE", first_retirement: "RETIRED"},
            kind=first_retirement,
        )

        with pytest.raises(CapacityError):
            store.allocate(first_retirement, op_id=f"{20:032x}")
        assert describe_tree(root) == before_refusal
        assert bounded_read(root / "authority" / "registry.log") == r_before
        assert bounded_read(root / "authority" / "committed-head.log") == h_before
        assert bounded_read(wal_path) == sentinel_before

        # The refused allocation did not consume the other kind's reservation.
        store.retire(tokens[second_retirement], op_id=f"{11:032x}")
        store.close_token(tokens[second_retirement])
        final = store.query()
        assert final.seq == 9
        assert final.state["JOURNAL"]["active"] is None
        assert final.state["WAL"]["active"] is None
        assert bounded_read(wal_path) == sentinel_before
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("case_id", "overrides"),
    [
        pytest.param(
            "P3-BOUNDARY-001-r-registry-minus-one",
            {"registry_byte_cap": 8 * 4096 - 1},
            id="P3-BOUNDARY-001-r-registry",
        ),
        pytest.param(
            "P3-BOUNDARY-002-r-head-minus-one",
            {"head_byte_cap": 8 * 2048 - 1},
            id="P3-BOUNDARY-002-r-head",
        ),
    ],
)
def test_one_byte_under_capacity_refuses_second_kind_before_any_write(
    case_id: str, overrides: dict[str, int]
) -> None:
    del case_id
    temporary, root = _new_root()
    store: SyntheticAuthorityStore | None = None
    try:
        limits = dict(DEFAULT_LIMITS)
        limits.update(overrides)
        store, _issuer = _fresh(root, limits=limits)
        journal = store.allocate("JOURNAL", op_id=f"{30:032x}")
        store.write(journal, b"JOURNAL_SENTINEL")
        before = describe_tree(root)
        assert (
            pair_capacity(
                CapacityLimits(
                    limits["registry_record_cap"],
                    limits["head_record_cap"],
                    limits["registry_byte_cap"],
                    limits["head_byte_cap"],
                )
            )
            == 7
        )
        assert not allocation_fits(
            limits=CapacityLimits(
                limits["registry_record_cap"],
                limits["head_record_cap"],
                limits["registry_byte_cap"],
                limits["head_byte_cap"],
            ),
            committed_pairs=4,
            phases={"JOURNAL": "ACTIVE", "WAL": "ABSENT"},
            kind="WAL",
        )
        with pytest.raises(CapacityError):
            store.allocate("WAL", op_id=f"{31:032x}")
        assert describe_tree(root) == before
        store.retire(journal, op_id=f"{32:032x}")
        store.close_token(journal)
        assert store.query().state["JOURNAL"]["active"] is None
    finally:
        if store is not None:
            try:
                store.close()
            except PoisonedError:
                pass
        temporary.cleanup()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p6_restore_001_replays_copied_old_bytes_and_issues_only_new_epoch() -> None:
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-restore-") as directory:
        temporary_root = Path(directory)
        temporary_root.chmod(0o700)
        source_root, archive_dir, issuer, evidence = _archive_source(temporary_root)
        target_parent = evidence["target_parent"]
        target_parent.mkdir(mode=0o700)
        target_parent.chmod(0o700)
        target_root = prepare_root(target_parent / "profile")
        original_anchor = evidence["source_anchor"]
        archive_before = evidence["archive_before"]

        restored = SyntheticAuthorityStore.enroll_verified_restore_test(target_root, issuer)
        try:
            assert issuer.restore_prepared
            assert issuer.grant is not None
            new_anchor, new_key = issuer.readback(issuer.grant.enrollment_id)
            snapshot = restored.snapshot()
            assert snapshot.seq == 1
            assert snapshot.component_state == "ENROLLED_TEST_ONLY"
            assert snapshot.state["JOURNAL"]["active"] is None
            assert snapshot.state["WAL"]["active"] is None
            assert new_anchor.enrollment_kind == "verified-restore-test"
            assert new_anchor.profile_id != original_anchor.profile_id
            assert new_anchor.instance_id != original_anchor.instance_id
            assert new_anchor.issuer_epoch != original_anchor.issuer_epoch
            assert new_anchor.registry_id != original_anchor.registry_id
            assert new_key != evidence["source_key"]
            with pytest.raises(AuthorityError):
                restored.open_token("WAL")
            assert describe_tree(archive_dir) == archive_before
            # The old descriptor inode was historical metadata. The copied
            # archive descriptor had a distinct inode and was authenticated by
            # its bytes/digest, without opening the old live source root.
            copied_c = observe_entry(archive_dir / "capabilities.frame")
            assert copied_c.inode != original_anchor.descriptor_ino
            assert copied_c.data == evidence["copied"]["descriptor"]
            assert source_root.exists()  # retained only as the unmodified source fixture
        finally:
            try:
                restored.close()
            except PoisonedError:
                pass


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param("P6-RESTORE-002-cut-binding", id="P6-RESTORE-002-cut-binding"),
        pytest.param("P6-RESTORE-003-copied-log-tamper", id="P6-RESTORE-003-log-tamper"),
        pytest.param("P6-RESTORE-004-old-key", id="P6-RESTORE-004-old-key"),
        pytest.param("P6-RESTORE-012-closure-declaration", id="P6-RESTORE-012-closure"),
        pytest.param("P6-RESTORE-013-sentinel-declaration", id="P6-RESTORE-013-sentinel"),
    ],
)
def test_inconsistent_copied_archive_refuses_before_target_or_add_only(
    case_id: str,
) -> None:
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-restore-bad-") as directory:
        temporary_root = Path(directory)
        temporary_root.chmod(0o700)
        _source_root, archive_dir, issuer, evidence = _archive_source(temporary_root)
        target_parent = evidence["target_parent"]
        target_parent.mkdir(mode=0o700)
        target_parent.chmod(0o700)
        target_root = prepare_root(target_parent / "profile")

        cut_fields = {
            "P6-RESTORE-002-cut-binding": "head_file_digest",
            "P6-RESTORE-012-closure-declaration": "closed_owner_inventory_digest",
            "P6-RESTORE-013-sentinel-declaration": "snapshot_sentinel_digest",
        }
        if case_id in cut_fields:
            bad_cut = dict(evidence["cut_payload"])
            bad_cut[cut_fields[case_id]] = "f" * 64
            issuer.cut_frame = encode_independent_frame("test-cut", bad_cut, issuer.cut_key)
        elif case_id == "P6-RESTORE-003-copied-log-tamper":
            damaged = bytearray(issuer.source_material["registry"])
            damaged[-1] ^= 1
            issuer.source_material["registry"] = bytes(damaged)
            log_fd = os.open(archive_dir / "registry.log", os.O_WRONLY)
            try:
                os.pwrite(log_fd, bytes((damaged[-1],)), len(damaged) - 1)
                os.fsync(log_fd)
            finally:
                os.close(log_fd)
        elif case_id == "P6-RESTORE-004-old-key":
            wrong = bytearray(issuer.source_key)
            wrong[0] ^= 1
            issuer.source_key = bytes(wrong)

        target_before = describe_tree(target_root)
        archive_before = describe_tree(archive_dir)
        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.enroll_verified_restore_test(target_root, issuer)

        assert issuer.restore_prepared
        assert issuer._item is None
        assert describe_tree(target_root) == target_before
        assert describe_tree(archive_dir) == archive_before


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    ("case_id", "reuse_field"),
    [
        pytest.param("P6-RESTORE-005-old-key-bytes", "key", id="P6-RESTORE-005-key-bytes"),
        pytest.param("P6-RESTORE-006-key-id", "key_id", id="P6-RESTORE-006-key-id"),
        pytest.param("P6-RESTORE-007-registry-id", "registry_id", id="P6-RESTORE-007-registry-id"),
        pytest.param(
            "P6-RESTORE-008-enrollment-id", "enrollment_id", id="P6-RESTORE-008-enrollment-id"
        ),
    ],
)
def test_verified_restore_never_reuses_historical_key_or_identity(
    case_id: str, reuse_field: str
) -> None:
    del case_id
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-restore-reuse-") as directory:
        temporary_root = Path(directory)
        temporary_root.chmod(0o700)
        _source_root, archive_dir, issuer, evidence = _archive_source(temporary_root)
        old_anchor = evidence["source_anchor"]
        issuer.fresh_overrides = {
            reuse_field: evidence["source_key"]
            if reuse_field == "key"
            else getattr(old_anchor, reuse_field)
        }
        target_parent = evidence["target_parent"]
        target_parent.mkdir(mode=0o700)
        target_parent.chmod(0o700)
        target_root = prepare_root(target_parent / "profile")
        target_before = describe_tree(target_root)
        archive_before = describe_tree(archive_dir)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.enroll_verified_restore_test(target_root, issuer)

        assert issuer.restore_prepared
        assert issuer._item is None
        assert describe_tree(target_root) == target_before
        assert describe_tree(archive_dir) == archive_before


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param("P6-RESTORE-009-cut-key-equals-old", id="P6-RESTORE-009-cut-old-key"),
        pytest.param("P6-RESTORE-010-cut-key-equals-new", id="P6-RESTORE-010-cut-new-key"),
    ],
)
def test_restore_cut_authentication_key_is_separate_from_both_epoch_keys(
    case_id: str,
) -> None:
    with tempfile.TemporaryDirectory(
        dir="/private/tmp", prefix="fna-restore-cut-key-"
    ) as directory:
        temporary_root = Path(directory)
        temporary_root.chmod(0o700)
        _source_root, archive_dir, issuer, evidence = _archive_source(temporary_root)
        if case_id.endswith("old"):
            issuer.cut_key = evidence["source_key"]
            issuer.cut_frame = encode_independent_frame(
                "test-cut", evidence["cut_payload"], issuer.cut_key
            )
        else:
            issuer.fresh_overrides = {"key": issuer.cut_key}

        target_parent = evidence["target_parent"]
        target_parent.mkdir(mode=0o700)
        target_parent.chmod(0o700)
        target_root = prepare_root(target_parent / "profile")
        target_before = describe_tree(target_root)
        archive_before = describe_tree(archive_dir)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.enroll_verified_restore_test(target_root, issuer)

        assert issuer._item is None
        assert describe_tree(target_root) == target_before
        assert describe_tree(archive_dir) == archive_before


@pytest.mark.skipif(
    sys.platform != "darwin", reason="positive filesystem proof requires Darwin ACL checks"
)
def test_p6_restore_011_pending_old_allocation_is_not_a_restore_source() -> None:
    with tempfile.TemporaryDirectory(
        dir="/private/tmp", prefix="fna-restore-pending-"
    ) as directory:
        temporary_root = Path(directory)
        temporary_root.chmod(0o700)
        _source_root, archive_dir, issuer, evidence = _archive_source(temporary_root, pending=True)
        assert evidence["pending"] is True
        target_parent = evidence["target_parent"]
        target_parent.mkdir(mode=0o700)
        target_parent.chmod(0o700)
        target_root = prepare_root(target_parent / "profile")
        target_before = describe_tree(target_root)
        archive_before = describe_tree(archive_dir)

        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.enroll_verified_restore_test(target_root, issuer)

        assert issuer._item is None
        assert describe_tree(target_root) == target_before
        assert describe_tree(archive_dir) == archive_before


@pytest.mark.skipif(sys.platform == "darwin", reason="Darwin is the only positive ACL profile")
def test_non_darwin_refuses_before_issuer_or_tree_effects(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-profile"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    gate = root / "profile-gate.lock"
    gate.write_bytes(b"")
    gate.chmod(0o600)
    before = describe_tree(root)
    issuer = InMemoryIssuer()

    with pytest.raises(UnsupportedPlatformError):
        SyntheticAuthorityStore.enroll_fresh_test(root, issuer)

    assert issuer.grant is None
    assert describe_tree(root) == before
