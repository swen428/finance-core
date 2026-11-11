"""Strict local journal integrity, without granting evidence or writer authority."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from ._recovery_rows_v1 import (
    COLUMNS,
    INTS,
    ClosureLimitsV1,
    decode_rows,
    exact,
    integer,
    limit,
    read_rows,
    rows_hash,
    sha,
    strict_json,
    text,
)
from .contracts import CommitReceipt, LifecycleError, RuntimeBinding, canonical, digest


class PreparedFinalizationError(LifecycleError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code, message):
    raise PreparedFinalizationError(code, message)


@dataclass(frozen=True)
class _Intent:
    body: dict
    intent_sha256: str
    packages: tuple[bytes, ...]
    outcome: str | None
    receipt: CommitReceipt | None


_BODY = {
    "format",
    "scope",
    "binding",
    "core_identity",
    "migration_identity",
    "migration_ledger_digest",
    "operation_id",
    "review_id",
    "action_evidence_id",
    "prepared_at",
    "expires_at",
    "before",
    "append",
    "packages",
    "projected_frontier",
    "projected_inventory",
}


def _assert_unreserved(conn, review_id, action_id, operation_id, *, reservation=None):
    rows = conn.execute(
        "SELECT operation_id FROM l2_finalization_intents "
        "WHERE operation_id=? OR review_id=? OR action_evidence_id=?",
        (operation_id, review_id, action_id),
    ).fetchall()
    if any(row[0] != reservation for row in rows):
        _fail("identity_reserved", "Operation, review or action is permanently reserved")


def _receipt(body):
    fact = body["append"]["fact"]
    return CommitReceipt(
        fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
    )


def _native_row(conn, name, row):
    if name == "events":
        where, values = "seq=?", (row["seq"],)
    elif name == "facts":
        where, values = "record_id=? AND version=?", (row["record_id"], row["version"])
    else:
        where, values = "operation_id=?", (row["operation_id"],)
    actual = conn.execute("SELECT * FROM l2_" + name + " WHERE " + where, values).fetchone()
    return None if actual is None else dict(actual)


def _prefix_rows(conn, event_seq, limits):
    """Read only the fixed historical prefix; later ordinary history is unbounded here."""
    conditions = {
        "events": "seq<=?",
        "facts": "commit_seq<=?",
        "decisions": "operation_id IN (SELECT operation_id FROM l2_events "
        "WHERE kind='confirm' AND seq<=?)",
    }
    for name, kind, key in (
        ("sources", "capture", "source_id"),
        ("proposals", "propose", "proposal_id"),
        ("reviews", "review", "review_id"),
        ("deliveries", "delivery", "review_id"),
    ):
        conditions[name] = (
            key + " IN (SELECT entity_id FROM l2_events WHERE kind='" + kind + "' AND seq<=?)"
        )
    values = {}
    total = 0
    for name, columns in COLUMNS.items():
        source = " FROM l2_" + name + " WHERE " + conditions[name]
        params = (event_seq,)
        count = conn.execute("SELECT count(*)" + source, params).fetchone()[0]
        total += count
        limit(total, limits.max_rows)
        if name == "events":
            limit(count, limits.max_events)
        for column in columns:
            if column not in INTS:
                maximum = conn.execute(
                    "SELECT coalesce(max(length(CAST(" + column + " AS BLOB))),0)" + source,
                    params,
                ).fetchone()[0]
                limit(maximum, limits.max_text_bytes)
        values[name] = [dict(row) for row in conn.execute("SELECT *" + source, params)]
    values["events"].sort(key=lambda row: row["seq"])
    event_order = {(row["kind"], row["entity_id"]): row["seq"] for row in values["events"]}
    for name, kind, key in (
        ("sources", "capture", "source_id"),
        ("proposals", "propose", "proposal_id"),
        ("reviews", "review", "review_id"),
        ("deliveries", "delivery", "review_id"),
    ):
        values[name].sort(key=lambda row: event_order.get((kind, row[key]), -1))
    values["facts"].sort(key=lambda row: row["commit_seq"])
    operations = {row["operation_id"]: row["seq"] for row in values["events"]}
    values["decisions"].sort(key=lambda row: operations.get(row["operation_id"], -1))
    return decode_rows(values, limits)


def _verify_journal(conn, *, limits=None):
    """Return validated local entries; authenticity remains a configured-service check."""
    limits = ClosureLimitsV1() if limits is None else limits
    try:
        count = conn.execute("SELECT count(*) FROM l2_finalization_intents").fetchone()[0]
        stored_size = conn.execute(
            "SELECT coalesce(sum(length(payload)),0) FROM l2_finalization_materials"
        ).fetchone()[0]
        if count > 1024 or stored_size > 64 * 1024 * 1024:
            _fail("journal_limit_exceeded", "Offline preparation journal capacity exceeded")
        materials = {}
        for row in conn.execute("SELECT package_sha256,payload FROM l2_finalization_materials"):
            sha(row[0])
            if type(row[1]) is not bytes or hashlib.sha256(row[1]).hexdigest() != row[0]:
                _fail("journal_mismatch", "Stored package bytes differ from content identity")
            if len(row[1]) > limits.max_package_bytes:
                _fail("journal_limit_exceeded", "Stored package exceeds configured limit")
            materials[row[0]] = row[1]
        outcomes = {
            row["operation_id"]: dict(row)
            for row in conn.execute("SELECT * FROM l2_finalization_outcomes")
        }
        result = {}
        referenced = set()
        pending = []
        if not count:
            if materials or outcomes:
                _fail("journal_mismatch", "Orphan finalization material or outcome")
            return result
        from ._recovery_material_engine import _required_evidence, _slice_rows
        from .recovery_material_v2 import decode_native_closure_v2

        for row in conn.execute("SELECT * FROM l2_finalization_intents ORDER BY operation_id"):
            body = exact(strict_json(row["body_json"].encode(), limits), _BODY)
            if (
                body["format"] != "prepared-native-finalization-intent-v1"
                or body["scope"] != "offline-synthetic-only-v1"
            ):
                _fail("journal_mismatch", "Unsupported preparation journal format or scope")
            exact(body["binding"], RuntimeBinding.__dataclass_fields__)
            RuntimeBinding(**body["binding"])
            for key in ("operation_id", "review_id", "action_evidence_id", "core_identity"):
                text(body[key], limits, identity=True)
            for key in ("migration_identity", "migration_ledger_digest"):
                sha(body[key])
            integer(body["prepared_at"], minimum=1)
            integer(body["expires_at"], minimum=1)
            if (
                body["prepared_at"] >= body["expires_at"]
                or row["created_at"] != body["prepared_at"]
                or any(
                    row[key] != body[key]
                    for key in ("operation_id", "review_id", "action_evidence_id")
                )
                or digest(body) != row["intent_sha256"]
            ):
                _fail("journal_mismatch", "Intent identity, time or digest mismatch")
            sha(row["intent_sha256"])
            original_review = conn.execute(
                "SELECT material_json FROM l2_reviews WHERE review_id=?", (body["review_id"],)
            ).fetchone()
            if original_review is None:
                _fail("journal_mismatch", "Intent review is missing")
            review = strict_json(original_review[0].encode(), limits)
            if (
                review["binding"] != body["binding"]
                or review["expires_at"] != body["expires_at"]
                or not review["created_at"] <= body["prepared_at"] < body["expires_at"]
            ):
                _fail("journal_mismatch", "Intent does not retain its exact review deadline")
            exact(body["before"], ("event_seq", "event_sha256", "rows_sha256", "evidence_sha256"))
            before = body["before"]
            integer(before["event_seq"])
            sha(before["event_sha256"], nullable=before["event_seq"] == 0)
            sha(before["rows_sha256"])
            sha(before["evidence_sha256"])
            exact(body["append"], ("event", "fact", "decision"))
            for key, table in (("event", "events"), ("fact", "facts"), ("decision", "decisions")):
                exact(body["append"][key], COLUMNS[table])
            append_rows = {name: [] for name in COLUMNS}
            for key, table in (("event", "events"), ("fact", "facts"), ("decision", "decisions")):
                append_rows[table] = [body["append"][key]]
            decode_rows(append_rows, limits)
            event, fact, decision = (body["append"][key] for key in ("event", "fact", "decision"))
            if (
                event["kind"] != "confirm"
                or event["operation_id"] != body["operation_id"]
                or event["seq"] != before["event_seq"] + 1
                or event["previous_sha256"] != before["event_sha256"]
                or event["entity_id"] != fact["record_id"]
                or fact["review_id"] != body["review_id"]
                or fact["commit_seq"] != event["seq"]
                or decision["operation_id"] != body["operation_id"]
                or decision["review_id"] != body["review_id"]
                or decision["action_evidence_id"] != body["action_evidence_id"]
                or decision["record_id"] != fact["record_id"]
                or decision["version"] != fact["version"]
            ):
                _fail("journal_mismatch", "Intent append identities disagree")
            payload = strict_json(event["payload_json"].encode(), limits)
            if (
                payload.get("accepted_at") != body["prepared_at"]
                or payload.get("review_id") != body["review_id"]
                or payload.get("action", {}).get("evidence_id") != body["action_evidence_id"]
                or payload.get("fact_sha256") != fact["fact_sha256"]
                or payload.get("version") != fact["version"]
            ):
                _fail("journal_mismatch", "Intent accepted action differs from exact append")
            hashes = body["packages"]
            if (
                type(hashes) is not list
                or not hashes
                or len(hashes) > limits.max_packages
                or len(hashes) != len(set(hashes))
            ):
                _fail("journal_mismatch", "Intent requires one complete ordered package chain")
            for item in hashes:
                sha(item)
            if any(item not in materials for item in hashes):
                _fail("journal_mismatch", "Intent package is missing")
            packages = tuple(materials[item] for item in hashes)
            if sum(map(len, packages)) > limits.max_chain_bytes:
                _fail("journal_limit_exceeded", "Intent package chain exceeds limit")
            referenced.update(hashes)
            segments = tuple(decode_native_closure_v2(item, limits=limits) for item in packages)
            rows_wire = {name: [] for name in COLUMNS}
            entries = []
            previous = None
            for package, segment in zip(packages, segments, strict=True):
                if segment.previous != previous:
                    _fail("journal_mismatch", "Stored material chain is discontinuous")
                identity = asdict(segment.identity)
                if (
                    any(
                        identity[key] != body[key]
                        for key in (
                            "core_identity",
                            "migration_identity",
                            "migration_ledger_digest",
                        )
                    )
                    or identity["source_binding"] != body["binding"]
                ):
                    _fail("journal_mismatch", "Intent and package installation or binding disagree")
                for name, values in segment.rows.wire().items():
                    rows_wire[name].extend(values)
                entries.extend(segment.evidence)
                previous = {
                    "package_sha256": hashlib.sha256(package).hexdigest(),
                    "event_seq": segment.last_event_seq,
                    "event_sha256": segment.last_event_sha256,
                    "format": "native-closure-frontier-v2",
                }
                # DTO comparison for the next segment, wire comparison below.
                from .recovery_material_v2 import ClosureFrontierV2

                previous = ClosureFrontierV2(**previous)
            all_rows = decode_rows(rows_wire, limits)
            tail = segments[-1]
            if (
                asdict(previous) != body["projected_frontier"]
                or canonical(asdict(tail.inventory)) != canonical(body["projected_inventory"])
                or tail.last_event_seq != event["seq"]
                or tail.last_event_sha256 != event["event_sha256"]
                or rows_wire["events"][-1] != event
                or fact not in rows_wire["facts"]
                or decision not in rows_wire["decisions"]
                or rows_hash(all_rows) != tail.inventory.rows_sha256
            ):
                _fail("journal_mismatch", "Intent projected material differs from exact append")
            prefix = _slice_rows(all_rows, 1, before["event_seq"], limits)
            actual_prefix = _prefix_rows(conn, before["event_seq"], limits)
            required = _required_evidence(prefix)
            old_entries = sorted(
                (entry for entry in entries if (entry.kind, entry.evidence_id) in required),
                key=lambda entry: (entry.kind, entry.evidence_id),
            )
            if (
                prefix != actual_prefix
                or rows_hash(prefix) != before["rows_sha256"]
                or digest([entry.wire() for entry in old_entries]) != before["evidence_sha256"]
                or (prefix.events[-1][-1] if prefix.events else None) != before["event_sha256"]
            ):
                _fail("journal_mismatch", "Intent no longer matches its retained source prefix")
            outcome = outcomes.pop(body["operation_id"], None)
            receipt = None
            if outcome is None:
                pending.append(body["operation_id"])
                current = read_rows(conn, limits)
                if len(current.events) != before["event_seq"] or current != prefix:
                    _fail("journal_mismatch", "Pending intent source frontier or inventory changed")
            else:
                if outcome["intent_sha256"] != row["intent_sha256"]:
                    _fail("journal_mismatch", "Outcome refers to another intent")
                integer(outcome["resolved_at"], minimum=1)
                if outcome["outcome"] == "APPLIED_OFFLINE":
                    receipt = _receipt(body)
                    if outcome["receipt_json"] != canonical(asdict(receipt)):
                        _fail("journal_mismatch", "Applied receipt differs from exact planned fact")
                    for key, table in (
                        ("event", "events"),
                        ("fact", "facts"),
                        ("decision", "decisions"),
                    ):
                        if _native_row(conn, table, body["append"][key]) != body["append"][key]:
                            _fail(
                                "journal_mismatch", "Applied journal lacks an exact native append"
                            )
                elif (
                    outcome["outcome"] != "CANCELLED_OFFLINE" or outcome["receipt_json"] is not None
                ):
                    _fail("journal_mismatch", "Unsupported finalization outcome")
            if outcome is None or outcome["outcome"] == "CANCELLED_OFFLINE":
                if (
                    conn.execute(
                        "SELECT 1 FROM l2_events WHERE operation_id=?", (body["operation_id"],)
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM l2_decisions WHERE operation_id=? "
                        "OR review_id=? OR action_evidence_id=?",
                        (body["operation_id"], body["review_id"], body["action_evidence_id"]),
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM l2_facts WHERE review_id=?", (body["review_id"],)
                    ).fetchone()
                ):
                    _fail(
                        "journal_mismatch",
                        "Unapplied intent unexpectedly owns native financial rows",
                    )
            result[body["operation_id"]] = _Intent(
                body,
                row["intent_sha256"],
                packages,
                outcome["outcome"] if outcome else None,
                receipt,
            )
        if outcomes or referenced != set(materials) or len(pending) > 1:
            _fail(
                "journal_mismatch",
                "Journal has orphan material, outcome or competing pending intent",
            )
        return result
    except PreparedFinalizationError:
        raise
    except (LifecycleError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise PreparedFinalizationError(
            "journal_mismatch", "Finalization journal is inconsistent"
        ) from exc
