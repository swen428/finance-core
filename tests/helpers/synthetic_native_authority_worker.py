"""Cold-process and hard-kill worker for synthetic authority tests only.

The worker is deliberately stored under tests/ and is not a packaged executable.
Its private channels carry only disposable test anchor/key material; secrets are
never command-line arguments or output in ordinary test logs.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import stat
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

import finance_core.synthetic_native_authority.store as store_module  # noqa: E402
from finance_core.synthetic_native_authority.store import (  # noqa: E402
    FreshTestGrant,
    SyntheticAuthorityStore,
    TestAnchor,
)

MAX_PRIVATE_MESSAGE = 32_768
MAX_STDOUT_MESSAGE = 65_536


class InMemoryIssuer:
    """Disposable add-only test port; it never persists the key to the tree."""

    def __init__(self) -> None:
        self.grant: FreshTestGrant | None = None
        self._item: tuple[TestAnchor, bytes] | None = None

    def prepare_fresh(self, root: Path) -> FreshTestGrant:
        del root
        if self.grant is None:
            self.grant = FreshTestGrant(
                installation_id=uuid.uuid4().hex,
                profile_id=uuid.uuid4().hex,
                instance_id=uuid.uuid4().hex,
                issuer_epoch=uuid.uuid4().hex,
                registry_id=uuid.uuid4().hex,
                key_id=uuid.uuid4().hex,
                enrollment_id=uuid.uuid4().hex,
                key=os.urandom(32),
            )
        return self.grant

    def add_only(self, anchor: TestAnchor, key: bytes) -> None:
        if self._item is not None:
            raise RuntimeError("test anchor already exists")
        self._item = (anchor, key)

    def readback(self, enrollment_id: str) -> tuple[TestAnchor, bytes]:
        if self._item is None or self._item[0].enrollment_id != enrollment_id:
            raise RuntimeError("test anchor is absent")
        return self._item


def prepare_root(root: Path) -> Path:
    """Create only the fixture-owned root and profile gate witness."""

    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    root.chmod(0o700)
    fd = os.open(root / "profile-gate.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return root


def _anchor_wire(anchor: TestAnchor) -> dict[str, Any]:
    value = asdict(anchor)
    value["root_chain"] = [list(item) for item in anchor.root_chain]
    return value


def _anchor_from_wire(value: dict[str, Any]) -> TestAnchor:
    value = dict(value)
    value["root_chain"] = tuple(
        (str(path), int(dev), int(ino)) for path, dev, ino in value["root_chain"]
    )
    return TestAnchor(**value)


def _read_bounded(stream: Any, limit: int = MAX_PRIVATE_MESSAGE) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("private test channel exceeded its bound")
    return data


def _write_fd(fd: int, payload: bytes) -> None:
    if len(payload) > MAX_PRIVATE_MESSAGE:
        raise ValueError("private test response exceeded its bound")
    view = memoryview(payload)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("private test response made no progress")
        view = view[count:]


class LeaseCheckingIssuer(InMemoryIssuer):
    """Fake enrollment port that probes the actual gate and lifecycle leases."""

    def __init__(self, root: Path, event_fd: int | None = None) -> None:
        super().__init__()
        self.root = root
        self.event_fd = event_fd

    def _assert_port_outside_leases(self, *, require_lifecycle: bool = False) -> None:
        gate = self.root / "profile-gate.lock"
        lifecycle = self.root / "authority" / "core-lifecycle.lock"
        paths = [(gate, True)]
        if require_lifecycle or lifecycle.exists():
            paths.append((lifecycle, require_lifecycle))
        for path, required in paths:
            if not path.exists():
                if required:
                    raise FileNotFoundError(path)
                continue
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError(f"invalid lease witness: {path.name}")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def prepare_fresh(self, root: Path) -> FreshTestGrant:
        self._assert_port_outside_leases()
        grant = super().prepare_fresh(root)
        if self.event_fd is not None:
            prepared = (
                json.dumps(
                    {
                        "kind": "prepared",
                        "key": base64.b64encode(grant.key).decode("ascii"),
                        "grant": {
                            field: getattr(grant, field)
                            for field in (
                                "installation_id",
                                "profile_id",
                                "instance_id",
                                "issuer_epoch",
                                "registry_id",
                                "key_id",
                                "enrollment_id",
                            )
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                + b"\n"
            )
            _write_fd(self.event_fd, prepared)
        return grant

    def add_only(self, anchor: TestAnchor, key: bytes) -> None:
        self._assert_port_outside_leases(require_lifecycle=True)
        super().add_only(anchor, key)
        if self.event_fd is not None:
            issued = (
                json.dumps(
                    {"kind": "issued", "anchor": _anchor_wire(anchor)},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                + b"\n"
            )
            _write_fd(self.event_fd, issued)

    def readback(self, enrollment_id: str) -> tuple[TestAnchor, bytes]:
        self._assert_port_outside_leases(require_lifecycle=True)
        return super().readback(enrollment_id)


def _snapshot_wire(store: SyntheticAuthorityStore) -> dict[str, Any]:
    snapshot = store.snapshot()
    return {
        "seq": snapshot.seq,
        "head_digest": snapshot.head_digest,
        "state": snapshot.state,
        "component_state": snapshot.component_state,
    }


def _seed(root: Path, result_fd: int) -> None:
    issuer = InMemoryIssuer()
    store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer)
    assert issuer.grant is not None
    for kind, offset in (("JOURNAL", 1), ("WAL", 10)):
        first = store.allocate(kind, op_id=f"{offset:032x}")
        store.write(first, f"retired-{kind}".encode("ascii"))
        store.retire(first, op_id=f"{offset + 1:032x}")
        store.close_token(first)
        current = store.allocate(kind, op_id=f"{offset + 2:032x}")
        store.write(current, f"active-{kind}".encode("ascii"))
        store.close_token(current)
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    store.close()
    message = json.dumps(
        {"anchor": _anchor_wire(anchor), "key": base64.b64encode(key).decode("ascii")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    _write_fd(result_fd, message)
    os.close(result_fd)


def _cold_reopen(root: Path) -> None:
    raw = _read_bounded(sys.stdin.buffer)
    request = json.loads(raw.decode("ascii"))
    anchor = _anchor_from_wire(request["anchor"])
    key = base64.b64decode(request["key"], validate=True)
    if len(key) != 32:
        raise ValueError("unexpected test key length")
    store = SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    try:
        encoded = json.dumps(_snapshot_wire(store), sort_keys=True, separators=(",", ":"))
        if len(encoded) > MAX_STDOUT_MESSAGE:
            raise ValueError("snapshot response exceeded its bound")
        sys.stdout.write(encoded + "\n")
    finally:
        store.close()


def _make_hook(
    event_fd: int,
    continue_fd: int,
    target_event: str,
    target_seq: int | None,
    target_phase: str | None,
    target_cursor: int | None,
    target_stage: str | None,
    target_role: str | None,
):
    def hook(event: str, evidence: dict[str, object]) -> None:
        if event != target_event:
            return
        if target_seq is not None and evidence.get("seq") != target_seq:
            return
        if target_phase is not None and evidence.get("phase") != target_phase:
            return
        if target_cursor is not None and evidence.get("cursor") != target_cursor:
            return
        if target_stage is not None and evidence.get("stage") != target_stage:
            return
        if target_role is not None and evidence.get("role") != target_role:
            return
        message = (
            json.dumps(
                {"kind": "hit", "event": event, "evidence": evidence},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        _write_fd(event_fd, message)
        os.read(continue_fd, 1)

    return hook


def _install_short_writes(chunk: int | None) -> None:
    if chunk is None:
        return
    if not 1 <= chunk <= 4096:
        raise ValueError("short-write chunk is out of bounds")
    original = os.pwrite

    def short_write(fd: int, data: bytes, offset: int) -> int:
        return original(fd, data[:chunk], offset)

    store_module.os.pwrite = short_write


def _crash_allocation(
    root: Path,
    event_fd: int,
    continue_fd: int,
    target_event: str,
    target_seq: int | None,
    target_phase: str | None,
    target_cursor: int | None,
    target_stage: str | None,
    target_role: str | None,
    short_chunk: int | None,
    kind: str,
    pre_retire_count: int,
) -> None:
    issuer = InMemoryIssuer()
    armed = False
    target_hook = _make_hook(
        event_fd,
        continue_fd,
        target_event,
        target_seq,
        target_phase,
        target_cursor,
        target_stage,
        target_role,
    )

    def hook(event: str, evidence: dict[str, object]) -> None:
        if armed:
            target_hook(event, evidence)

    store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer, hook=hook)
    assert issuer.grant is not None
    if not 0 <= pre_retire_count <= 2:
        raise ValueError("pre-retire count is out of bounds")
    for index in range(pre_retire_count):
        old = store.allocate(kind, op_id=f"{90 + 2 * index:032x}")
        store.write(old, f"RETIRED_SLOT_{index}_SENTINEL".encode("ascii"))
        store.retire(old, op_id=f"{91 + 2 * index:032x}")
        store.close_token(old)
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    ready = (
        json.dumps(
            {
                "kind": "ready",
                "anchor": _anchor_wire(anchor),
                "key": base64.b64encode(key).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    _write_fd(event_fd, ready)
    os.read(continue_fd, 1)
    _install_short_writes(short_chunk)
    armed = True
    store.allocate(kind, op_id=f"{99:032x}")
    store.close()


def _crash_enrollment(
    root: Path,
    event_fd: int,
    continue_fd: int,
    target_event: str,
    target_seq: int | None,
    target_phase: str | None,
    target_cursor: int | None,
    target_stage: str | None,
    target_role: str | None,
    short_chunk: int | None,
) -> None:
    _install_short_writes(short_chunk)
    SyntheticAuthorityStore.enroll_fresh_test(
        root,
        LeaseCheckingIssuer(root, event_fd),
        hook=_make_hook(
            event_fd,
            continue_fd,
            target_event,
            target_seq,
            target_phase,
            target_cursor,
            target_stage,
            target_role,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "reopen", "crash-allocation", "crash-enrollment"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--result-fd", type=int)
    parser.add_argument("--event-fd", type=int)
    parser.add_argument("--continue-fd", type=int)
    parser.add_argument("--target-event")
    parser.add_argument("--target-seq", type=int)
    parser.add_argument("--target-phase")
    parser.add_argument("--target-cursor", type=int)
    parser.add_argument("--target-stage")
    parser.add_argument("--target-role")
    parser.add_argument("--short-chunk", type=int)
    parser.add_argument("--kind", choices=("JOURNAL", "WAL"), default="JOURNAL")
    parser.add_argument("--pre-retire-count", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root)
    if args.mode == "seed":
        if args.result_fd is None:
            parser.error("seed requires --result-fd")
        _seed(root, args.result_fd)
    elif args.mode == "reopen":
        _cold_reopen(root)
    elif args.mode == "crash-allocation":
        if args.event_fd is None or args.continue_fd is None or not args.target_event:
            parser.error("crash-allocation requires event, continue and target arguments")
        _crash_allocation(
            root,
            args.event_fd,
            args.continue_fd,
            args.target_event,
            args.target_seq,
            args.target_phase,
            args.target_cursor,
            args.target_stage,
            args.target_role,
            args.short_chunk,
            args.kind,
            args.pre_retire_count,
        )
    else:
        if args.event_fd is None or args.continue_fd is None or not args.target_event:
            parser.error("crash-enrollment requires event, continue and target arguments")
        _crash_enrollment(
            root,
            args.event_fd,
            args.continue_fd,
            args.target_event,
            args.target_seq,
            args.target_phase,
            args.target_cursor,
            args.target_stage,
            args.target_role,
            args.short_chunk,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"synthetic authority test worker failed: {type(exc).__name__}\n")
        raise SystemExit(2)
