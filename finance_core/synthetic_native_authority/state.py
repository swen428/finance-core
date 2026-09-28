"""Pure synthetic slot history and finite-capacity rules."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .format import FormatError, state_digest


class StateError(ValueError):
    """An event violates the frozen synthetic state machine."""


def initial_state() -> dict[str, Any]:
    return {
        kind: {
            "last_generation": 0,
            "last_slot": None,
            "active": None,
            "pending": None,
            "slots": [{"generation": 0, "phase": "VIRGIN"} for _ in range(2)],
        }
        for kind in ("JOURNAL", "WAL")
    }


def validate_state(value: Any, generation_cap: int = 63) -> None:
    if type(value) is not dict or set(value) != {"JOURNAL", "WAL"}:
        raise StateError("state kinds")
    for kind in ("JOURNAL", "WAL"):
        entry = value[kind]
        if type(entry) is not dict or set(entry) != {
            "last_generation",
            "last_slot",
            "active",
            "pending",
            "slots",
        }:
            raise StateError("state fields")
        generation = entry["last_generation"]
        if type(generation) is not int or not 0 <= generation <= generation_cap:
            raise StateError("last generation")
        if entry["last_slot"] is not None and (
            type(entry["last_slot"]) is not int or entry["last_slot"] not in (0, 1)
        ):
            raise StateError("last slot")
        if type(entry["slots"]) is not list or len(entry["slots"]) != 2:
            raise StateError("slot set")
        for slot in entry["slots"]:
            if type(slot) is not dict or set(slot) != {"generation", "phase"}:
                raise StateError("slot fields")
            if type(slot["generation"]) is not int or not 0 <= slot["generation"] <= generation:
                raise StateError("slot generation")
            if slot["phase"] not in ("VIRGIN", "RESET_INTENT", "RESET_DONE", "ACTIVE", "RETIRED"):
                raise StateError("slot phase")
            if slot["phase"] == "VIRGIN" and slot["generation"] != 0:
                raise StateError("virgin generation")
        active, pending = entry["active"], entry["pending"]
        if active is not None and pending is not None:
            raise StateError("active and pending")
        if active is not None:
            if type(active) is not dict or set(active) != {"generation", "slot"}:
                raise StateError("active fields")
            _match(entry, active, "ACTIVE")
        if pending is not None:
            if type(pending) is not dict or set(pending) != {
                "generation",
                "slot",
                "phase",
                "op_id",
            }:
                raise StateError("pending fields")
            if pending["phase"] not in ("RESET_INTENT", "RESET_DONE"):
                raise StateError("pending phase")
            _match(entry, pending, pending["phase"])


def _match(entry: dict[str, Any], ref: dict[str, Any], phase: str) -> None:
    slot = ref["slot"]
    if type(slot) is not int or slot not in (0, 1) or type(ref["generation"]) is not int:
        raise StateError("reference type")
    if ref["generation"] != entry["last_generation"] or entry["slots"][slot] != {
        "generation": ref["generation"],
        "phase": phase,
    }:
        raise StateError("reference mismatch")


def capacity(limits: dict[str, int]) -> int:
    return min(
        limits["registry_record_cap"],
        limits["head_record_cap"],
        limits["registry_byte_cap"] // 4096,
        limits["head_byte_cap"] // 2048,
    )


def reservations(state: dict[str, Any]) -> dict[str, int]:
    result = {}
    for kind in ("JOURNAL", "WAL"):
        entry = state[kind]
        pending = entry["pending"]
        result[kind] = (
            1
            if entry["active"] is not None
            else (3 if pending and pending["phase"] == "RESET_INTENT" else (2 if pending else 0))
        )
    return result


def assert_reservations(state: dict[str, Any], seq: int, limits: dict[str, int]) -> None:
    validate_state(state, limits["generation_cap"])
    room = capacity(limits) - seq
    if room < sum(reservations(state).values()):
        raise StateError("retirement capacity exhausted")


def select_slot(state: dict[str, Any], kind: str, referenced: set[tuple[str, int, int]]) -> int:
    if kind not in ("JOURNAL", "WAL"):
        raise StateError("kind")
    entry = state[kind]
    if entry["active"] is not None or entry["pending"] is not None:
        raise StateError("kind occupied")
    eligible = [
        index
        for index, slot in enumerate(entry["slots"])
        if slot["phase"] in ("VIRGIN", "RETIRED")
        and not any(k == kind and s == index for k, _g, s in referenced)
    ]
    if not eligible:
        raise StateError("no unreferenced slot")
    other = 1 - entry["last_slot"] if entry["last_slot"] is not None else 0
    return other if other in eligible else eligible[0]


def transition(
    state: dict[str, Any],
    event: str,
    kind: str | None,
    generation: int,
    slot: int | None,
    op_id: str,
    generation_cap: int = 63,
) -> dict[str, Any]:
    validate_state(state, generation_cap)
    if event == "GENESIS":
        if state != initial_state() or kind is not None or slot is not None or generation != 0:
            raise StateError("genesis")
        return initial_state()
    if kind not in ("JOURNAL", "WAL") or type(slot) is not int or slot not in (0, 1):
        raise StateError("event target")
    result = deepcopy(state)
    entry = result[kind]
    if event == "RESET_INTENT":
        if entry["active"] is not None or entry["pending"] is not None:
            raise StateError("occupied")
        if generation != entry["last_generation"] + 1 or generation > generation_cap:
            raise StateError("generation increment")
        if entry["slots"][slot]["phase"] not in ("VIRGIN", "RETIRED"):
            raise StateError("slot not reusable")
        entry["last_generation"] = generation
        entry["last_slot"] = slot
        entry["slots"][slot] = {"generation": generation, "phase": "RESET_INTENT"}
        entry["pending"] = {
            "generation": generation,
            "slot": slot,
            "phase": "RESET_INTENT",
            "op_id": op_id,
        }
    elif event in ("RESET_DONE", "ACTIVE"):
        pending = entry["pending"]
        expected_phase = "RESET_INTENT" if event == "RESET_DONE" else "RESET_DONE"
        if pending != {
            "generation": generation,
            "slot": slot,
            "phase": expected_phase,
            "op_id": op_id,
        }:
            raise StateError("pending transition")
        entry["slots"][slot]["phase"] = event
        if event == "RESET_DONE":
            pending["phase"] = event
        else:
            entry["pending"] = None
            entry["active"] = {"generation": generation, "slot": slot}
    elif event == "RETIRED":
        if entry["active"] != {"generation": generation, "slot": slot}:
            raise StateError("stale retirement")
        entry["active"] = None
        entry["slots"][slot]["phase"] = "RETIRED"
    else:
        raise StateError("event")
    validate_state(result, generation_cap)
    return result


def apply_record(
    state: dict[str, Any], record: dict[str, Any], limits: dict[str, int]
) -> dict[str, Any]:
    if record["prior_state_digest"] != (
        "0" * 64 if record["event"] == "GENESIS" else state_digest(state)
    ):
        raise FormatError("prior state digest")
    result = transition(
        state,
        record["event"],
        record["kind"],
        record["generation"],
        record["slot"],
        record["op_id"],
        limits["generation_cap"],
    )
    if state_digest(result) != record["result_state_digest"]:
        raise FormatError("result state digest")
    assert_reservations(result, record["seq"], limits)
    return result
