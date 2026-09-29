"""Independent slot I/O fault and fixed-boundary probes for synthetic authority."""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import pytest

from finance_core.synthetic_native_authority.format import DEFAULT_LIMITS
from finance_core.synthetic_native_authority.store import (
    AuthorityError,
    CapacityError,
    PoisonedError,
    SyntheticAuthorityStore,
)

HELPERS = Path(__file__).resolve().parent / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from synthetic_native_authority_worker import InMemoryIssuer, prepare_root  # noqa: E402

WORKER = HELPERS / "synthetic_native_authority_worker.py"


def _fresh(
    *, limits: dict[str, int] | None = None
) -> tuple[tempfile.TemporaryDirectory[str], Path, SyntheticAuthorityStore, object]:
    temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-io-")
    parent = Path(temporary.name)
    parent.chmod(0o700)
    root = prepare_root(parent / "profile")
    store = SyntheticAuthorityStore.enroll_fresh_test(root, InMemoryIssuer(), limits=limits)
    token = store.allocate("WAL", op_id=f"{1:032x}")
    return temporary, root, store, token


def _close(store: SyntheticAuthorityStore, temporary: tempfile.TemporaryDirectory[str]) -> None:
    with suppress(PoisonedError):
        store.close()
    temporary.cleanup()


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_slot_short_writes_finish_in_one_call_without_extra_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary, root, store, token = _fresh()
    try:
        slot = root / "slots" / "wal-0.slot"
        original = os.pwrite
        target = store._fds["wal0"]
        calls = 0

        def short_write(fd: int, data: bytes, offset: int) -> int:
            nonlocal calls
            if fd == target:
                calls += 1
                return original(fd, data[:2], offset)
            return original(fd, data, offset)

        monkeypatch.setattr(os, "pwrite", short_write)
        before = store.snapshot()
        store.write(token, b"COMMITTED_WAL_SENTINEL")
        assert calls > 1
        assert slot.read_bytes() == b"COMMITTED_WAL_SENTINEL"
        assert store.snapshot() == before
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("fault", ["zero", "enospc"])
def test_p5_slot_write_fault_has_no_false_success_or_followup_effect(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    temporary, root, store, token = _fresh()
    try:
        slot = root / "slots" / "wal-0.slot"
        registry = root / "authority" / "registry.log"
        head = root / "authority" / "committed-head.log"
        before = (slot.read_bytes(), registry.read_bytes(), head.read_bytes())
        original = os.pwrite
        target = store._fds["wal0"]

        def fail_slot(fd: int, data: bytes, offset: int) -> int:
            if fd == target:
                if fault == "zero":
                    return 0
                raise OSError(errno.ENOSPC, "modeled slot is full")
            return original(fd, data, offset)

        monkeypatch.setattr(os, "pwrite", fail_slot)
        with pytest.raises((PoisonedError, OSError)):
            store.write(token, b"MUST_NOT_ACK")
        assert (slot.read_bytes(), registry.read_bytes(), head.read_bytes()) == before
        with pytest.raises(PoisonedError):
            store.snapshot()
        with pytest.raises(PoisonedError):
            store.write(token, b"SECOND_WRITE")
        assert (slot.read_bytes(), registry.read_bytes(), head.read_bytes()) == before
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("fault", ["eof", "fsync"])
def test_p5_slot_read_or_barrier_fault_poison_preserves_bytes(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    temporary, root, store, token = _fresh()
    try:
        store.write(token, b"COMMITTED_WAL_SENTINEL")
        slot = root / "slots" / "wal-0.slot"
        before = slot.read_bytes()
        target = store._fds["wal0"]
        if fault == "eof":
            original_read = os.pread

            def short_read(fd: int, size: int, offset: int) -> bytes:
                if fd == target:
                    return b""
                return original_read(fd, size, offset)

            monkeypatch.setattr(os, "pread", short_read)
            with pytest.raises(PoisonedError):
                store.read(token, size=1)
        else:
            original_sync = os.fsync

            def fail_sync(fd: int) -> None:
                if fd == target:
                    raise OSError(errno.EIO, "modeled slot barrier failure")
                original_sync(fd)

            monkeypatch.setattr(os, "fsync", fail_sync)
            with pytest.raises(OSError):
                store.sync(token)
        assert slot.read_bytes() == before
        with pytest.raises(PoisonedError):
            store.snapshot()
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_token_operation_and_slot_caps_refuse_before_effect() -> None:
    limits = dict(DEFAULT_LIMITS)
    limits["token_cap"] = 1
    limits["operation_byte_cap"] = 4
    limits["slot_byte_cap"] = 5
    temporary, root, store, token = _fresh(limits=limits)
    try:
        slot = root / "slots" / "wal-0.slot"
        before = slot.read_bytes()
        with pytest.raises(CapacityError):
            store.open_token("WAL")
        with pytest.raises(CapacityError):
            store.write(token, b"12345")
        with pytest.raises(CapacityError):
            store.write(token, b"12", offset=4)
        assert slot.read_bytes() == before
        store.write(token, b"1234")
        assert slot.read_bytes() == b"1234"
        store.close_token(token)
        second = store.open_token("WAL")
        assert store.read(second, size=4) == b"1234"
        store.close_token(second)
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_expired_operation_deadline_refuses_without_slot_effect() -> None:
    temporary, root, store, token = _fresh()
    try:
        slot = root / "slots" / "wal-0.slot"
        before = slot.read_bytes()

        # The test fault hook simulates an overlong protected operation after
        # the public dispatcher has started it and before its first slot write.
        def expire_at_slot(event: str, _evidence: dict[str, object]) -> None:
            if event == "before_slot_write":
                store._deadline = time.monotonic() - 1

        store._hook = expire_at_slot
        with pytest.raises(PoisonedError):
            store.write(token, b"MUST_NOT_WRITE")
        assert slot.read_bytes() == before
        with pytest.raises(PoisonedError):
            store.write(token, b"SECOND_WRITE")
        assert slot.read_bytes() == before
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_cross_thread_owner_call_refuses_without_poison_or_effect() -> None:
    temporary, root, store, token = _fresh()
    try:
        slot = root / "slots" / "wal-0.slot"
        before = slot.read_bytes()
        errors: list[BaseException] = []

        def foreign_thread() -> None:
            try:
                store.write(token, b"WRONG_THREAD")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=foreign_thread)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], AuthorityError)
        assert slot.read_bytes() == before
        assert store.snapshot().seq == 4
        store.write(token, b"OWNER_THREAD")
        assert slot.read_bytes() == b"OWNER_THREAD"
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_reentrant_hook_cannot_start_second_public_operation() -> None:
    temporary, root, store, token = _fresh()
    try:
        slot = root / "slots" / "wal-0.slot"
        nested_errors: list[BaseException] = []

        def nested_query(event: str, _evidence: dict[str, object]) -> None:
            if event == "before_slot_write":
                try:
                    store.snapshot()
                except BaseException as exc:
                    nested_errors.append(exc)

        store._hook = nested_query
        store.write(token, b"OUTER_WRITE_ONLY")
        assert len(nested_errors) == 1 and isinstance(nested_errors[0], AuthorityError)
        assert slot.read_bytes() == b"OUTER_WRITE_ONLY"
        assert store.snapshot().seq == 4
    finally:
        _close(store, temporary)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("phase", ["before_kernel_close", "after_kernel_close"])
def test_p5_uncertain_close_never_retries_number_and_reap_allows_cold_owner(
    phase: str,
) -> None:
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-close-") as location:
        parent = Path(location)
        parent.chmod(0o700)
        root = prepare_root(parent / "profile")
        sentinel = parent / "reused-fd.sentinel"
        script = textwrap.dedent(
            """
            import base64
            import errno
            import json
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            sys.path.insert(0, sys.argv[2])
            from synthetic_native_authority_worker import InMemoryIssuer, _anchor_wire
            from finance_core.synthetic_native_authority.store import (
                PoisonedError, SyntheticAuthorityStore,
            )

            root, sentinel, phase = Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
            issuer = InMemoryIssuer()
            store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer)
            token = store.allocate('WAL', op_id=f'{1:032x}')
            store.write(token, b'CLOSE_UNCERTAIN_WAL')
            anchor, key = issuer.readback(issuer.grant.enrollment_id)
            target = store._fds['wal0']
            original_close = os.close
            reused = None
            filler = []
            fault_calls = 0

            def uncertain_close(fd):
                global reused, fault_calls
                if fd == target:
                    fault_calls += 1
                    if fault_calls != 1:
                        raise AssertionError('close retried uncertain FD number')
                    if phase == 'before_kernel_close':
                        raise OSError(errno.EIO, 'modeled uncertain close before kernel close')
                    original_close(fd)
                    # Other owned FDs may already have closed; fill those holes
                    # until the just-closed number is reused by this sentinel.
                    for _ in range(64):
                        candidate = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o600)
                        filler.append(candidate)
                        if candidate == target:
                            reused = candidate
                            break
                    if reused is None:
                        raise AssertionError('FD number was not reused within bound')
                    os.write(reused, b'REUSED_FD_SENTINEL')
                    raise OSError(errno.EIO, 'modeled uncertain close after kernel close')
                original_close(fd)

            os.close = uncertain_close
            try:
                try:
                    store.close()
                except PoisonedError:
                    pass
                else:
                    raise AssertionError('uncertain close reported success')
            finally:
                os.close = original_close
            assert fault_calls == 1
            if phase == 'after_kernel_close':
                assert reused is not None and os.fstat(reused).st_size == 18
                assert sentinel.read_bytes() == b'REUSED_FD_SENTINEL'
            else:
                assert reused is None and os.fstat(target).st_size == 19
            try:
                SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
            except PoisonedError:
                blocked = True
            else:
                blocked = False
            assert blocked
            for number in filler:
                original_close(number)
            print(json.dumps({
                'blocked_before_reap': blocked,
                'sentinel': sentinel.read_bytes().decode('ascii') if sentinel.exists() else None,
                'fault_calls': fault_calls,
                'anchor': _anchor_wire(anchor),
                'key': base64.b64encode(key).decode('ascii'),
            }, sort_keys=True))
            """
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path.cwd()),
                str(HELPERS),
                str(root),
                str(sentinel),
                phase,
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
        result = json.loads(child.stdout)
        assert result["blocked_before_reap"] is True
        assert result["fault_calls"] == 1
        if phase == "after_kernel_close":
            assert result["sentinel"] == "REUSED_FD_SENTINEL"
            assert sentinel.read_bytes() == b"REUSED_FD_SENTINEL"
        else:
            assert result["sentinel"] is None and not sentinel.exists()
        request = json.dumps({"anchor": result["anchor"], "key": result["key"]}).encode("ascii")
        cold = subprocess.run(
            [sys.executable, str(WORKER), "reopen", "--root", str(root)],
            input=request,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert cold.returncode == 0, cold.stderr.decode("utf-8", "replace")
        snapshot = json.loads(cold.stdout)
        assert snapshot["seq"] == 4
        assert snapshot["state"]["WAL"]["active"] == {"generation": 1, "slot": 0}
        assert (root / "slots" / "wal-0.slot").read_bytes() == b"CLOSE_UNCERTAIN_WAL"


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("substitute", ["fifo", "symlink", "hardlink"])
def test_p4_cold_reopen_refuses_special_registry_without_touching_either_inode(
    substitute: str,
) -> None:
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-special-") as location:
        parent = Path(location)
        parent.chmod(0o700)
        root = prepare_root(parent / "profile")
        issuer = InMemoryIssuer()
        store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer)
        token = store.allocate("JOURNAL", op_id=f"{1:032x}")
        store.close_token(token)
        assert issuer.grant is not None
        anchor, key = issuer.readback(issuer.grant.enrollment_id)
        store.close()

        registry = root / "authority" / "registry.log"
        original = parent / "registry.original"
        os.rename(registry, original)
        original_bytes = original.read_bytes()
        if substitute == "fifo":
            os.mkfifo(registry, mode=0o600)
        elif substitute == "symlink":
            os.symlink(original, registry)
        else:
            os.link(original, registry)

        def witness(path: Path) -> tuple[int, int, int, int, int, str | None]:
            item = os.lstat(path)
            return (
                item.st_dev,
                item.st_ino,
                stat.S_IFMT(item.st_mode),
                item.st_size,
                item.st_nlink,
                os.readlink(path) if stat.S_ISLNK(item.st_mode) else None,
            )

        before_original = witness(original)
        before_substitute = witness(registry)
        request = json.dumps(
            {"anchor": asdict(anchor), "key": base64.b64encode(key).decode("ascii")}
        ).encode("ascii")
        cold = subprocess.run(
            [sys.executable, str(WORKER), "reopen", "--root", str(root)],
            input=request,
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert cold.returncode == 2, cold.stderr.decode("utf-8", "replace")
        assert b"QuarantinedError" in cold.stderr
        assert original.read_bytes() == original_bytes
        assert witness(original) == before_original
        assert witness(registry) == before_substitute
