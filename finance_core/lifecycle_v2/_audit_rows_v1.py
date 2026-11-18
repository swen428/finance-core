"""Private immutable raw rows for one already protected source audit.

Rows are data, never authentication or admission results. Original semantic
validators and every external evidence callback still use the real connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

from finance_core.instance_admission.sessions import (
    _ProductionConnection,
    _submit_admission,
    _witness_for,
)

from .contracts import LifecycleError

# Optimization budgets only: a larger complete inventory uses original SQL.
_MAX_ROWS = 32_768
_MAX_BYTES = 32 * 1024 * 1024
_COLUMNS = {
    "sources": "source_id source_key evidence_id principal_id client_id raw_input "
    "original_sha256 source_sha256 generation created_at",
    "proposals": "proposal_id source_id version observation_id expected_version record_id "
    "values_json observation_json proposal_sha256 created_at",
    "reviews": "review_id proposal_id expected_version record_id values_json material_json "
    "review_sha256 created_at",
    "deliveries": "review_id evidence_id material_json",
    "facts": "record_id version economic_event_id source_id review_id values_json "
    "predecessor_sha256 fact_sha256 commit_seq",
    "decisions": "action_evidence_id review_id operation_id record_id version material_json",
    "events": "seq operation_id kind entity_id payload_json previous_sha256 event_sha256",
}
# Named finite lookups, not SQL interception or a substitute connection.
_KEYS = {
    "source": ("sources", ("source_id",), True),
    "proposal": ("proposals", ("proposal_id",), True),
    "proposal_version": ("proposals", ("source_id", "version"), True),
    "source_proposals": ("proposals", ("source_id",), False),
    "review": ("reviews", ("review_id",), True),
    "delivery": ("deliveries", ("review_id",), True),
    "fact_version": ("facts", ("record_id", "version"), True),
    "record_facts": ("facts", ("record_id",), False),
    "source_facts": ("facts", ("source_id",), False),
    "commit_facts": ("facts", ("commit_seq",), False),
    "decision_version": ("decisions", ("record_id", "version"), True),
    "action_decision": ("decisions", ("action_evidence_id",), True),
    "record_decisions": ("decisions", ("record_id",), False),
    "operation_decisions": ("decisions", ("operation_id",), False),
    "event": ("events", ("seq",), True),
    "entity_events": ("events", ("kind", "entity_id"), False),
    "operation_events": ("events", ("kind", "operation_id"), False),
}


@dataclass(frozen=True, slots=True)
class _RawRow(Mapping):
    columns: tuple
    data: tuple

    def __getitem__(self, key):
        return self.data[key if type(key) is int else self.columns.index(key)]

    def __iter__(self):
        return iter(self.columns)

    def __len__(self):
        return len(self.columns)


def _copy(row):
    return _RawRow(tuple(row.keys()), tuple(row))


def _size(row):
    return sum(len(str(value).encode("utf-8")) for value in row.data if value is not None)


def _read(conn, sql):
    cursor = conn.execute(sql)
    try:
        return tuple(cursor.fetchall())
    finally:
        cursor.close()


def _metadata(conn):
    parts = []
    for table, columns in _COLUMNS.items():
        size = "+".join(f"coalesce(length(CAST({key} AS BLOB)),0)" for key in columns.split())
        parts.append(f"SELECT '{table}',count(*),coalesce(sum({size}),0) FROM l2_{table}")
    return tuple(tuple(row) for row in _read(conn, " UNION ALL ".join(parts)))


def _indexes(tables):
    indexes = {}
    for name, (table, columns, unique) in _KEYS.items():
        groups = {}
        for row in tables[table]:
            key = tuple(row[column] for column in columns)
            group = groups.setdefault(key, [])
            if unique and group:
                raise LifecycleError("Duplicate audit row index identity")
            group.append(row)
        indexes[name] = MappingProxyType({key: tuple(group) for key, group in groups.items()})
    return MappingProxyType(indexes)


def _reconcile(tables, metadata):
    if set(tables) != set(_COLUMNS) or any(
        row.columns != tuple(_COLUMNS[name].split())
        for name, rows in tables.items()
        for row in rows
    ):
        raise LifecycleError("Incomplete audit row inventory")
    actual = tuple(
        (name, len(tables[name]), sum(_size(row) for row in tables[name])) for name in _COLUMNS
    )
    if actual != metadata:
        raise LifecycleError("Complete audit row inventory changed during loading")


def _prefix_state(indexes, record_id, source_id, cutoff):
    facts = indexes["record_facts"].get((record_id,), ())
    current = max(
        (row for row in facts if cutoff is None or row["commit_seq"] < cutoff),
        key=lambda row: row["version"],
        default=None,
    )
    proposals = indexes["source_proposals"].get((source_id,), ())
    eligible = (
        row
        for row in proposals
        if any(
            cutoff is None or event["seq"] < cutoff
            for event in indexes["entity_events"].get(("propose", row["proposal_id"]), ())
        )
    )
    latest = max(eligible, key=lambda row: row["version"], default=None)
    return current, latest


class _AuditRows:
    def __init__(self, conn, witness, tables, vectors):
        self._conn, self._witness = conn, witness
        self._changes = conn.total_changes
        self._tables = MappingProxyType(tables)
        self._indexes = _indexes(tables)
        self._vectors = MappingProxyType(vectors)
        self._daily_projections = {}
        self._active = True

    def _check(self, conn):
        witness = self._witness
        if (
            not self._active
            or conn is not self._conn
            or _witness_for(conn) is not witness
            or not witness.active
            or witness.closed
            or witness.closing
            or witness.owner._active is not witness
            or not conn.in_transaction
            or conn.total_changes != self._changes
            or witness.evidence_callback_token is not None
        ):
            self.invalidate()
            raise LifecycleError("Expired or changed audit row scope")

    def table(self, conn, name):
        self._check(conn)
        return self._tables[name]

    def vector(self, conn, name):
        self._check(conn)
        return self._vectors[name]

    def many(self, conn, name, *key):
        self._check(conn)
        return self._indexes[name].get(key, ())

    def one(self, conn, name, *key):
        rows = self.many(conn, name, *key)
        if len(rows) > 1:
            raise LifecycleError("Audit lookup requires one exact row")
        return rows[0] if rows else None

    def project(self, conn, name, columns, *key):
        row = self.one(conn, name, *key)
        return tuple(row[column] for column in columns) if row is not None else None

    def project_many(self, conn, name, columns, *key):
        return tuple(
            tuple(row[column] for column in columns) for row in self.many(conn, name, *key)
        )

    def facts_before(self, conn, cutoff):
        return tuple(row for row in self.table(conn, "facts") if row["commit_seq"] < cutoff)

    def state_at(self, conn, record_id, source_id, cutoff):
        self._check(conn)
        return _prefix_state(self._indexes, record_id, source_id, cutoff)

    def daily_projection(self, conn, name, registry, cutoff, raw, build):
        # View identity binds all retained input bytes. Each named projection
        # keeps only its last cut and exact extra raw input. Builders return
        # immutable row data and never call an evidence or admission port.
        self._check(conn)
        key = (registry, cutoff, raw)
        previous = self._daily_projections.get(name)
        if previous is not None and previous[0] == key:
            return previous[1]
        result = build()
        self._daily_projections[name] = (key, result)
        return result

    def finish(self, conn):
        # Full current permission proof in the original read/write mode, even
        # when the last validation work touched only immutable Python values.
        _submit_admission(conn)
        self._check(conn)

    def invalidate(self):
        self._active = False
        self._daily_projections.clear()
        self._tables = self._indexes = self._vectors = MappingProxyType({})


@contextmanager
def audit_rows(conn, port):
    """Choose SQL cleanly before callbacks, or own exactly one complete view."""
    from .daily_v1.accounts import _EvidenceReadPort

    witness = _witness_for(conn)
    if (
        type(conn) is not _ProductionConnection
        or witness is None
        or type(port) is not _EvidenceReadPort
        or not conn.in_transaction
        or witness.evidence_callback_token is not None
    ):
        yield None
        return
    _submit_admission(conn)
    excluded = _read(
        conn,
        "SELECT EXISTS(SELECT 1 FROM l2_imports),EXISTS(SELECT 1 FROM l2_archives),"
        "EXISTS(SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive')),"
        "EXISTS(SELECT 1 FROM l2_sources WHERE source_key LIKE 'legacy:%')",
    )[0]
    if any(excluded):
        yield None
        return
    metadata = _metadata(conn)
    if sum(row[1] for row in metadata) > _MAX_ROWS or sum(row[2] for row in metadata) > _MAX_BYTES:
        yield None
        return
    changes = conn.total_changes
    tables = {}
    view = None
    try:
        for name in _COLUMNS:
            order = (
                " ORDER BY seq"
                if name == "events"
                else (" ORDER BY record_id,version" if name == "facts" else "")
            )
            tables[name] = tuple(
                _copy(row) for row in _read(conn, f"SELECT * FROM l2_{name}{order}")
            )
        _reconcile(tables, metadata)
        vectors = {
            "sources": tuple(row[0] for row in _read(conn, "SELECT source_id FROM l2_sources")),
            "records": tuple(
                row[0] for row in _read(conn, "SELECT DISTINCT record_id FROM l2_facts")
            ),
            "owned_records": tuple(
                row[0]
                for row in _read(
                    conn, "SELECT record_id FROM l2_facts UNION SELECT record_id FROM l2_decisions"
                )
            ),
        }
        view = _AuditRows(conn, witness, tables, vectors)
        if conn.total_changes != changes:
            raise LifecycleError("Audit row loading changed protected state")
        view._check(conn)
        yield view
        view.finish(conn)
    finally:
        if view is not None:
            view.invalidate()
