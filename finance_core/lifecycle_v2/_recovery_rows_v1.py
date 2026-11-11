"""Private fixed row and strict wire codec for native closure material."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from typing import Any

from .contracts import LifecycleError, canonical


class RecoveryMaterialError(LifecycleError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fail(code: str, message: str) -> Any:
    raise RecoveryMaterialError(code, message)


@dataclass(frozen=True)
class ClosureLimitsV1:
    max_package_bytes: int = 16 * 1024 * 1024
    max_chain_bytes: int = 64 * 1024 * 1024
    max_packages: int = 32
    max_events: int = 10_000
    max_rows: int = 20_000
    max_evidence_entries: int = 10_000
    max_artifact_bytes: int = 4 * 1024 * 1024
    max_artifact_total_bytes: int = 32 * 1024 * 1024
    max_text_bytes: int = 1024 * 1024
    max_json_depth: int = 64

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 1 <= value <= field.default:
                fail("invalid_material", "Limits must be positive, exact, lowering-only integers")


def limits_v1(value):
    if value is None:
        return ClosureLimitsV1()
    if type(value) is not ClosureLimitsV1:
        fail("invalid_material", "Exact ClosureLimitsV1 required")
    value.__post_init__()
    return value


def limit(value: int, maximum: int):
    if value > maximum:
        fail("material_limit_exceeded", "Native closure exceeds configured material limits")


def integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        fail("invalid_material", "Exact bounded integer required")
    return value


def text(value, limits, *, identity=False):
    if type(value) is not str:
        fail("invalid_material", "Exact text required")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise RecoveryMaterialError("invalid_material", "Invalid Unicode text") from exc
    limit(size, limits.max_text_bytes)
    if identity and (not 1 <= size <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        fail("invalid_material", "Bounded identity without control characters required")
    return value


def sha(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        fail("invalid_material", "Exact lowercase SHA-256 required")
    return value


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        fail("invalid_material", "Material field set differs from versioned contract")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("invalid_material", "Duplicate JSON key")
        result[key] = value
    return result


def _not_number(value):
    fail("invalid_material", "Floating-point and nonfinite JSON numbers are forbidden")


def _walk(value, limits, depth=0, path=()):
    limit(depth, limits.max_json_depth)
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 2**63 - 1:
            fail("invalid_material", "JSON integer outside signed 64-bit range")
    elif type(value) is str:
        archive_path = path
        if len(path) >= 2 and path[0] == "evidence" and type(path[1]) is int:
            archive_path = path[2:]
        if (
            len(archive_path) == 3
            and archive_path[0] == "artifacts"
            and type(archive_path[1]) is int
            and archive_path[2] == "bytes_b64"
        ):
            limit(len(value), 4 * ((limits.max_artifact_bytes + 2) // 3))
        elif archive_path == ("authentication_b64",):
            limit(len(value), 4 * ((65536 + 2) // 3))
        else:
            text(value, limits)
    elif type(value) is list:
        for index, item in enumerate(value):
            _walk(item, limits, depth + 1, path + (index,))
    elif type(value) is dict:
        for key, item in value.items():
            text(key, limits)
            _walk(item, limits, depth + 1, path + (key,))
    else:
        fail("invalid_material", "Unsupported JSON value")


def strict_json(payload, limits, *, maximum=None):
    if type(payload) is not bytes:
        fail("invalid_material", "Exact material bytes required")
    limit(len(payload), maximum or limits.max_package_bytes)
    try:
        raw = payload.decode("utf-8", errors="strict")
        value = json.loads(
            raw, object_pairs_hook=_pairs, parse_float=_not_number, parse_constant=_not_number
        )
        _walk(value, limits)
        if canonical(value).encode("utf-8") != payload:
            fail("invalid_material", "Material JSON must be byte-exact canonical UTF-8")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RecoveryMaterialError):
            raise
        raise RecoveryMaterialError("invalid_material", "Invalid material JSON") from exc


# These are trusted code constants, never names/SQL selected by the wire.
COLUMNS = {
    "sources": (
        "source_id",
        "source_key",
        "evidence_id",
        "principal_id",
        "client_id",
        "raw_input",
        "original_sha256",
        "source_sha256",
        "generation",
        "created_at",
    ),
    "proposals": (
        "proposal_id",
        "source_id",
        "version",
        "observation_id",
        "expected_version",
        "record_id",
        "values_json",
        "observation_json",
        "proposal_sha256",
        "created_at",
    ),
    "reviews": (
        "review_id",
        "proposal_id",
        "expected_version",
        "record_id",
        "values_json",
        "material_json",
        "review_sha256",
        "created_at",
    ),
    "deliveries": ("review_id", "evidence_id", "material_json"),
    "facts": (
        "record_id",
        "version",
        "economic_event_id",
        "source_id",
        "review_id",
        "values_json",
        "predecessor_sha256",
        "fact_sha256",
        "commit_seq",
    ),
    "decisions": (
        "action_evidence_id",
        "review_id",
        "operation_id",
        "record_id",
        "version",
        "material_json",
    ),
    "events": (
        "seq",
        "operation_id",
        "kind",
        "entity_id",
        "payload_json",
        "previous_sha256",
        "event_sha256",
    ),
}
TABLES = {key: "l2_" + key for key in COLUMNS}
SELECTS = {
    key: "SELECT " + ",".join(cols) + " FROM " + TABLES[key] for key, cols in COLUMNS.items()
}
INSERTS = {
    key: "INSERT INTO "
    + TABLES[key]
    + "("
    + ",".join(cols)
    + ") VALUES("
    + ",".join("?" for _ in cols)
    + ")"
    for key, cols in COLUMNS.items()
}
INTS = {"created_at", "version", "expected_version", "commit_seq", "seq"}
NULLABLE = {"record_id", "review_id", "predecessor_sha256", "previous_sha256"}
KINDS = {
    "capture": "sources",
    "propose": "proposals",
    "review": "reviews",
    "delivery": "deliveries",
    "confirm": "facts",
}


@dataclass(frozen=True)
class NativeRowsV1:
    sources: tuple = ()
    proposals: tuple = ()
    reviews: tuple = ()
    deliveries: tuple = ()
    facts: tuple = ()
    decisions: tuple = ()
    events: tuple = ()

    def wire(self):
        return {
            name: [dict(zip(COLUMNS[name], row, strict=True)) for row in getattr(self, name)]
            for name in COLUMNS
        }


def decode_rows(value, limits):
    exact(value, COLUMNS)
    result = {}
    total = 0
    for name, columns in COLUMNS.items():
        if type(value[name]) is not list:
            fail("invalid_material", "Row collection must be an array")
        total += len(value[name])
        limit(total, limits.max_rows)
        rows = []
        for row in value[name]:
            exact(row, columns)
            for key, cell in row.items():
                if key in INTS:
                    integer(cell, minimum=1 if key in {"version", "seq", "commit_seq"} else 0)
                elif cell is None:
                    if key not in NULLABLE:
                        fail("invalid_material", "Unexpected null row cell")
                else:
                    text(cell, limits, identity=not (key.endswith("_json") or key == "raw_input"))
                    if key.endswith("_sha256"):
                        sha(cell)
                    if key.endswith("_json"):
                        strict_json(cell.encode("utf-8"), limits, maximum=limits.max_text_bytes)
            rows.append(tuple(row[key] for key in columns))
        result[name] = tuple(rows)
    limit(len(result["events"]), limits.max_events)
    return NativeRowsV1(**result)


def row_dicts(rows):
    return rows.wire()


def owned_rows(rows, first, last):
    """Require a bijective event owner for every row, in exact owning order."""
    wire = rows.wire()
    events = wire["events"]
    if len(events) != last - first + 1 or any(
        event["seq"] != first + index for index, event in enumerate(events)
    ):
        fail("chain_mismatch", "Segment events are not its exact contiguous sequence")
    owners = {key: [] for key in COLUMNS if key != "events"}
    used = {key: set() for key in owners}
    for event in events:
        kind = event["kind"]
        if kind not in KINDS:
            fail("unsupported_scope", "Only native lifecycle events are supported")
        name = KINDS[kind]
        if kind == "confirm":
            matches = [r for r in wire[name] if r["commit_seq"] == event["seq"]]
            decisions = [r for r in wire["decisions"] if r["operation_id"] == event["operation_id"]]
            if len(decisions) != 1:
                fail("inventory_mismatch", "Confirm lacks its one owned decision")
            decision = decisions[0]
            marker = decision["action_evidence_id"]
            if marker in used["decisions"]:
                fail("inventory_mismatch", "Duplicate decision owner")
            used["decisions"].add(marker)
            owners["decisions"].append(decision)
        else:
            key = {"sources": "source_id", "proposals": "proposal_id"}.get(name, "review_id")
            matches = [r for r in wire[name] if r[key] == event["entity_id"]]
        if len(matches) != 1:
            fail("inventory_mismatch", "Event lacks its one exact owned row")
        row = matches[0]
        marker = canonical(row)
        if marker in used[name]:
            fail("inventory_mismatch", "Duplicate row owner")
        used[name].add(marker)
        owners[name].append(row)
    for name, expected in owners.items():
        if expected != wire[name]:
            fail("inventory_mismatch", "Rows differ from complete event-owned order")
    return wire


def read_rows(conn, limits):
    """Preflight row/text capacities before materializing bounded native tables."""
    total = 0
    for name, columns in COLUMNS.items():
        count = conn.execute("SELECT count(*) FROM " + TABLES[name]).fetchone()[0]
        total += count
        limit(total, limits.max_rows)
        if name == "events":
            limit(count, limits.max_events)
        for column in columns:
            if column in INTS:
                continue
            # Both table and column are fixed implementation constants.
            maximum = conn.execute(
                "SELECT coalesce(max(length(CAST(" + column + " AS BLOB))),0) FROM " + TABLES[name]
            ).fetchone()[0]
            limit(maximum, limits.max_text_bytes)
    values = {name: [dict(row) for row in conn.execute(sql)] for name, sql in SELECTS.items()}
    events = sorted(values["events"], key=lambda row: row["seq"])
    values["events"] = events
    order = {kind: {} for kind in KINDS}
    for event in events:
        if event["kind"] not in KINDS:
            fail("unsupported_scope", "Only native lifecycle event kinds supported")
        order[event["kind"]][event["entity_id"]] = event["seq"]
    for name, kind, key in (
        ("sources", "capture", "source_id"),
        ("proposals", "propose", "proposal_id"),
        ("reviews", "review", "review_id"),
        ("deliveries", "delivery", "review_id"),
    ):
        values[name].sort(key=lambda row: order[kind].get(row[key], -1))
    values["facts"].sort(key=lambda row: row["commit_seq"])
    decision_order = {event["operation_id"]: event["seq"] for event in events}
    values["decisions"].sort(key=lambda row: decision_order.get(row["operation_id"], -1))
    return decode_rows(values, limits)


def rows_hash(rows):
    return hashlib.sha256(canonical(rows.wire()).encode("utf-8")).hexdigest()
