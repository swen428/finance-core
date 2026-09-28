"""Independent byte observations and finite-capacity model for D4 U1-B tests.

This helper deliberately does not import ``finance_core.synthetic_native_authority``.
It encodes only the public plan's fixed frame sizes and reservation table so the
tests can compare implementation effects with an independently computed result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

FRAME_SIZES = {
    "descriptor": 16_384,
    "registry": 4_096,
    "head": 2_048,
    "receipt": 2_048,
    "test-cut": 4_096,
}
FRAME_SPECS = {
    "descriptor": (b"FNA1CAP\n", 16_384, b"finance-core/synthetic-authority/v1/capability\0"),
    "registry": (b"FNA1REG\n", 4_096, b"finance-core/synthetic-authority/v1/registry\0"),
    "head": (b"FNA1HED\n", 2_048, b"finance-core/synthetic-authority/v1/head\0"),
    "receipt": (b"FNA1RCP\n", 2_048, b"finance-core/synthetic-authority/v1/receipt\0"),
    "test-cut": (
        b"FNA1TST\n",
        4_096,
        b"finance-core/synthetic-authority/v1/test-cut-certificate\0",
    ),
}
REGISTRY_FRAME_SIZE = FRAME_SIZES["registry"]
HEAD_FRAME_SIZE = FRAME_SIZES["head"]
DEFAULT_MAX_FILE_BYTES = 1_048_576

RETIREMENT_RESERVATION = {
    "ABSENT": 0,
    "RETIRED": 0,
    "ACTIVE": 1,
    "RESET_INTENT": 3,
    "RESET_DONE": 2,
}


class ReferenceMachine:
    """Small table-driven public-history model independent of production code."""

    def __init__(self) -> None:
        self.seq = 1  # the durable GENESIS pair is already present
        self.state: dict[str, dict[str, object]] = {
            kind: {
                "last_generation": 0,
                "last_slot": None,
                "active": None,
                "pending": None,
                "slots": [
                    {"generation": 0, "phase": "VIRGIN"},
                    {"generation": 0, "phase": "VIRGIN"},
                ],
            }
            for kind in ("JOURNAL", "WAL")
        }
        self.references: Counter[tuple[str, int, int]] = Counter()

    def snapshot_state(self) -> dict[str, dict[str, object]]:
        return deepcopy(self.state)

    def _eligible_slots(self, kind: str) -> list[int]:
        entry = self.state[kind]
        slots = entry["slots"]
        assert isinstance(slots, list)
        eligible: list[int] = []
        for index, slot in enumerate(slots):
            assert isinstance(slot, dict)
            if slot["phase"] not in {"VIRGIN", "RETIRED"}:
                continue
            if any(
                k == kind and s == index and count for (k, _g, s), count in self.references.items()
            ):
                continue
            eligible.append(index)
        return eligible

    def allocate(self, kind: str, limits: CapacityLimits) -> tuple[int, int]:
        if kind not in {"JOURNAL", "WAL"}:
            raise ValueError("kind must be JOURNAL or WAL")
        entry = self.state[kind]
        if entry["active"] is not None or entry["pending"] is not None:
            raise ValueError("kind is occupied in the reference model")
        phases = {
            current: (
                "ACTIVE"
                if state["active"] is not None
                else "RESET_DONE"
                if state["pending"] is not None and state["pending"]["phase"] == "RESET_DONE"
                else "RESET_INTENT"
                if state["pending"] is not None
                else "RETIRED"
                if state["last_generation"]
                else "ABSENT"
            )
            for current, state in self.state.items()
        }
        if pair_capacity(limits) - self.seq < required_future_pairs(phases) + 4:
            raise ValueError("allocation exceeds modeled reserved capacity")
        eligible = self._eligible_slots(kind)
        if not eligible:
            raise ValueError("no modeled slot is free of retained references")
        preferred = 0 if entry["last_slot"] is None else 1 - int(entry["last_slot"])
        slot = preferred if preferred in eligible else eligible[0]
        generation = int(entry["last_generation"]) + 1
        if generation > 63:
            raise ValueError("modeled generation limit")
        entry["last_generation"] = generation
        entry["last_slot"] = slot
        slot_rows = entry["slots"]
        assert isinstance(slot_rows, list)
        slot_rows[slot] = {"generation": generation, "phase": "ACTIVE"}
        entry["active"] = {"generation": generation, "slot": slot}
        self.seq += 3  # INTENT, DONE, ACTIVE
        reference = (kind, generation, slot)
        self.references[reference] += 1
        return generation, slot

    def open_active(self, kind: str) -> tuple[str, int, int]:
        active = self.state[kind]["active"]
        if active is None:
            raise ValueError("kind is absent in the reference model")
        reference = (kind, int(active["generation"]), int(active["slot"]))
        self.references[reference] += 1
        return reference

    def retire(self, kind: str) -> tuple[int, int]:
        entry = self.state[kind]
        active = entry["active"]
        if active is None:
            raise ValueError("kind is absent in the reference model")
        generation, slot = int(active["generation"]), int(active["slot"])
        entry["active"] = None
        slot_rows = entry["slots"]
        assert isinstance(slot_rows, list)
        slot_rows[slot] = {"generation": generation, "phase": "RETIRED"}
        self.seq += 1
        return generation, slot

    def release(self, reference: tuple[str, int, int]) -> None:
        if self.references[reference] <= 0:
            raise ValueError("reference model close without a retained token")
        self.references[reference] -= 1
        if self.references[reference] == 0:
            del self.references[reference]

    def matches(self, snapshot: object) -> bool:
        return getattr(snapshot, "seq") == self.seq and getattr(snapshot, "state") == self.state


@dataclass(frozen=True)
class CapacityLimits:
    """Public limits copied into a fresh synthetic descriptor."""

    registry_record_cap: int
    head_record_cap: int
    registry_byte_cap: int
    head_byte_cap: int


@dataclass(frozen=True)
class FileObservation:
    """Bounded, non-following observation of one directory entry."""

    kind: str
    mode: int
    device: int
    inode: int
    links: int
    size: int
    sha256: str | None
    data: bytes | None


def pair_capacity(limits: CapacityLimits) -> int:
    """Return the independent upper bound on complete R/H pairs."""

    values = (
        limits.registry_record_cap,
        limits.head_record_cap,
        limits.registry_byte_cap // REGISTRY_FRAME_SIZE,
        limits.head_byte_cap // HEAD_FRAME_SIZE,
    )
    return min(values)


def canonical_digest(value: object) -> str:
    """Hash the documented compact sorted-key JSON representation independently."""

    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def encode_independent_frame(
    kind: str, payload: object, key: bytes, *, raw: bytes | None = None
) -> bytes:
    """Build an authenticated test vector using only the published wire contract."""

    magic, frame_size, domain = FRAME_SPECS[kind]
    body_payload = (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
        if raw is None
        else raw
    )
    if len(key) != 32 or not 2 <= len(body_payload) <= frame_size - 44:
        raise ValueError("invalid independent test frame input")
    body = magic + len(body_payload).to_bytes(4, "big") + body_payload
    body += bytes(frame_size - 32 - len(body))
    return body + hmac.digest(key, domain + body, "sha256")


def required_future_pairs(phases: Mapping[str, str]) -> int:
    """Count reserved completion/retirement pairs from the frozen phase table."""

    try:
        return sum(RETIREMENT_RESERVATION[phase] for phase in phases.values())
    except KeyError as exc:
        raise ValueError(f"unknown modeled phase: {exc.args[0]}") from exc


def allocation_fits(
    *,
    limits: CapacityLimits,
    committed_pairs: int,
    phases: Mapping[str, str],
    kind: str,
    live_references: int = 0,
) -> bool:
    """Check the whole allocation plus protected retirement room before INTENT."""

    if kind not in {"JOURNAL", "WAL"}:
        raise ValueError("kind must be JOURNAL or WAL")
    if phases.get(kind, "ABSENT") not in {"ABSENT", "RETIRED"} or live_references:
        return False
    capacity = pair_capacity(limits)
    free_pairs = capacity - committed_pairs
    other_phases = {name: phase for name, phase in phases.items() if name != kind}
    return free_pairs >= required_future_pairs(other_phases) + 4


def bounded_read(path: Path, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> bytes:
    """Read one regular file through a no-follow FD and enforce a hard byte cap."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular file: {path.name}")
        if before.st_size > max_bytes:
            raise ValueError(f"file exceeds observation bound: {path.name}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if len(data) > max_bytes or len(data) != before.st_size:
            raise ValueError(f"file changed or exceeds observation bound: {path.name}")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(f"file identity changed during observation: {path.name}")
        return data
    finally:
        os.close(fd)


def observe_entry(path: Path, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> FileObservation:
    """Capture the visible entry identity and bytes without following symlinks."""

    entry = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(entry.st_mode):
        kind = "symlink"
        data = None
    elif stat.S_ISREG(entry.st_mode):
        kind = "regular"
        data = bounded_read(path, max_bytes=max_bytes)
    elif stat.S_ISDIR(entry.st_mode):
        kind = "directory"
        data = None
    else:
        kind = "special"
        data = None
    return FileObservation(
        kind=kind,
        mode=stat.S_IMODE(entry.st_mode),
        device=entry.st_dev,
        inode=entry.st_ino,
        links=entry.st_nlink,
        size=entry.st_size,
        sha256=None if data is None else hashlib.sha256(data).hexdigest(),
        data=data,
    )


def frame_count(data: bytes, frame_size: int) -> int:
    """Count only exact complete fixed-size frames; reject every trailing byte."""

    if frame_size <= 0 or len(data) % frame_size:
        raise ValueError("log does not contain only complete fixed-size frames")
    return len(data) // frame_size


def describe_tree(root: Path, *, max_entries: int = 128) -> dict[str, FileObservation]:
    """Observe a small synthetic tree in stable path order, without following links."""

    found: dict[str, FileObservation] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        for child in children:
            relative = Path(child.path).relative_to(root).as_posix()
            if len(found) >= max_entries:
                raise ValueError("synthetic tree exceeds observation entry bound")
            path = Path(child.path)
            observed = observe_entry(path)
            found[relative] = observed
            if observed.kind == "directory":
                stack.append(path)
    return dict(sorted(found.items()))
