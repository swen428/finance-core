"""One-audit immutable inventory, original prefix inputs and real SQL parity."""

import json
import sqlite3
import sys
from dataclasses import asdict, replace

import pytest

from finance_core.instance_admission import InstanceAdmissionError
from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.instance_admission.sessions import _witness_for, lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2._audit_rows_v1 import (
    _COLUMNS,
    _indexes,
    _prefix_state,
    _RawRow,
    _reconcile,
    _size,
    audit_rows,
)
from finance_core.lifecycle_v2.contracts import canonical
from finance_core.lifecycle_v2.daily_v1._prefix_v1 import validate_order
from finance_core.lifecycle_v2.daily_v1.accounts import (
    _EvidenceReadPort,
    _state_at,
    registered_values,
)
from finance_core.lifecycle_v2.daily_v1.contracts import decode_expected
from finance_core.lifecycle_v2.reader import _audit_evidence_connection, audit_evidence_connection
from tests.test_daily_accounts_lifecycle_v1 import change, create, setup
from tests.test_lifecycle_v2 import live as live


def raw(table, **values):
    columns = tuple(_COLUMNS[table].split())
    return _RawRow(columns, tuple(values.get(key) for key in columns))


def empty_tables():
    return {name: () for name in _COLUMNS}


def test_pure_complete_inventory_collision_multiplicity_and_original_bytes():
    tables = empty_tables()
    original = '{ "untouched": "原文" }'
    event = raw("events", seq=1, kind="unknown", entity_id="event-only", payload_json=original)
    decision = raw("decisions", record_id="decision-only", version=1, operation_id="op")
    tables["events"] = (event,)
    tables["decisions"] = (decision,)
    metadata = tuple((name, len(rows), sum(map(_size, rows))) for name, rows in tables.items())
    _reconcile(tables, metadata)
    index = _indexes(tables)
    assert index["entity_events"][("unknown", "event-only")] == (event,)
    assert index["record_decisions"][("decision-only",)] == (decision,)
    assert event["payload_json"] == original
    assert index["commit_facts"].get((1,), ()) == ()
    assert index["review"].get(("missing-counterpart",), ()) == ()
    with pytest.raises(LifecycleError, match="unsupported_record_format"):
        registered_values({"format": "unregistered-future-domain"})
    with pytest.raises(LifecycleError, match="prefix order"):
        validate_order("confirm", (1, 2, 3, 3, 5))
    with pytest.raises(TypeError):
        index["event"][(1,)] = ()
    with pytest.raises(TypeError):
        event["payload_json"] = "changed"
    with pytest.raises(LifecycleError, match="Incomplete"):
        _reconcile({name: rows for name, rows in tables.items() if name != "sources"}, metadata)
    with pytest.raises(LifecycleError, match="changed"):
        _reconcile(tables, tuple((name, count + 1, size) for name, count, size in metadata))
    tables["events"] = (event, event)
    with pytest.raises(LifecycleError, match="Duplicate"):
        _indexes(tables)
    # Nonunique counterpart rows remain present for original exact-cardinality checks.
    tables["events"] = (event, replace(event, data=(2,) + event.data[1:]))
    assert len(_indexes(tables)["entity_events"][("unknown", "event-only")]) == 2


def test_pure_prefix_uses_strict_event_cutoff_and_proposal_version():
    tables = empty_tables()
    tables["facts"] = tuple(
        raw("facts", record_id="r", source_id="s", version=v, commit_seq=seq)
        for v, seq in ((1, 10), (2, 30), (3, 50))
    )
    tables["proposals"] = tuple(
        raw("proposals", proposal_id=f"p{v}", source_id="s", version=v) for v in (3, 1, 2)
    )
    tables["events"] = tuple(
        raw("events", seq=seq, kind="propose", entity_id=f"p{v}")
        for v, seq in ((1, 5), (2, 20), (3, 40))
    )
    indexes = _indexes(tables)
    for cutoff in (1, 5, 6, 10, 11, 20, 21, 30, 31, 40, 41, 50, 51, None):
        fact, proposal = _prefix_state(indexes, "r", "s", cutoff)
        expected_facts = [r for r in tables["facts"] if cutoff is None or r["commit_seq"] < cutoff]
        expected_ids = {
            r["entity_id"] for r in tables["events"] if cutoff is None or r["seq"] < cutoff
        }
        expected_proposals = [r for r in tables["proposals"] if r["proposal_id"] in expected_ids]
        assert fact == max(expected_facts, key=lambda r: r["version"], default=None)
        assert proposal == max(expected_proposals, key=lambda r: r["version"], default=None)


class TracePort:
    def __init__(self, port, *, changed_repeat=False):
        self.port = port
        self.changed_repeat = changed_repeat
        self.trace = []
        self.tokens = []
        self.cursors = []

    def __getattr__(self, name):
        def call(conn, identity, expected=None):
            witness = _witness_for(conn)
            assert witness is not None and witness.evidence_callback_token is not None
            self.tokens.append(witness.evidence_callback_token)
            cursor = conn.execute("SELECT count(*) FROM l2_events")
            assert cursor.fetchone()[0] > 0
            self.cursors.append(cursor)
            method = getattr(self.port, name)
            result = (
                method(conn, identity) if expected is None else method(conn, identity, expected)
            )
            if (
                self.changed_repeat
                and name == "source"
                and any(row[:2] == (name, identity) for row in self.trace)
            ):
                result = replace(result, raw_input=result.raw_input + "changed")
            self.trace.append(
                (
                    name,
                    identity,
                    canonical(asdict(expected)) if expected is not None else None,
                    canonical(asdict(result)),
                    id(conn),
                )
            )
            return result

        return call


def test_same_real_connection_sql_index_trace_prefix_and_no_writes(live):
    ledger, port, daily, reader, _ = setup(live)
    _, _, review, first = create(ledger, port, daily)
    view = reader.get_account(first.account_ref.account_id)
    change(port, daily, view, "account_rename", {"name": "Renamed"}, "rename")
    observer = TracePort(port)
    protected = _EvidenceReadPort(observer)
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        try:
            before = _logical_database_digest(conn), conn.total_changes
            sql = _audit_evidence_connection(conn, protected, ledger.binding)
            sql_trace = tuple(observer.trace)
            observer.trace.clear()
            with audit_rows(conn, protected) as rows:
                assert rows is not None
                indexed = _audit_evidence_connection(conn, protected, ledger.binding, _rows=rows)
                assert tuple(observer.trace) == sql_trace
                for proposal in rows.table(conn, "proposals"):
                    expected = decode_expected(json.loads(proposal["observation_json"])["expected"])
                    for cutoff in (1, 2, 5, 6, 10, 11, None):
                        assert _state_at(conn, expected, cutoff, _rows=rows) == _state_at(
                            conn, expected, cutoff
                        )
                assert tuple(rows.vector(conn, "sources")) == tuple(
                    r[0] for r in conn.execute("SELECT source_id FROM l2_sources")
                )
            assert indexed == sql
            with pytest.raises(LifecycleError, match="Expired"):
                rows.table(conn, "facts")
            assert (_logical_database_digest(conn), conn.total_changes) == before
            observer.trace.clear()
            assert audit_evidence_connection(conn, protected, ledger.binding) == sql
            assert tuple(observer.trace) == sql_trace
            assert len({id(token) for token in observer.tokens}) == len(observer.tokens)
            for cursor in observer.cursors:
                with pytest.raises(InstanceAdmissionError, match="Expired"):
                    cursor.fetchone()
        finally:
            conn.rollback()
    assert (
        daily.lookup_commit_receipt(
            "confirm1", review_id=review.review_id, action_evidence_id="action1"
        )
        == first
    )


def test_changed_repeated_evidence_is_called_and_refused_without_retry(live):
    ledger, port, daily, _, _ = setup(live)
    create(ledger, port, daily)
    traces = []
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        try:
            before = _logical_database_digest(conn), conn.total_changes
            for indexed in (False, True):
                observer = TracePort(port, changed_repeat=True)
                protected = _EvidenceReadPort(observer)
                with pytest.raises(LifecycleError, match="Source/original evidence changed"):
                    if indexed:
                        with audit_rows(conn, protected) as rows:
                            assert rows is not None
                            _audit_evidence_connection(conn, protected, ledger.binding, _rows=rows)
                    else:
                        _audit_evidence_connection(conn, protected, ledger.binding)
                traces.append(tuple(observer.trace))
            assert traces[0] == traces[1]
            assert len(traces[0]) == 2
            assert (_logical_database_digest(conn), conn.total_changes) == before
            with pytest.raises(LifecycleError, match="Expired"):
                rows.table(conn, "sources")
        finally:
            conn.rollback()


def test_selector_sql_fallback_without_adapter_transaction_or_production(live, monkeypatch):
    ledger, port, daily, _, _ = setup(live)
    with sqlite3.connect(":memory:") as raw_conn, audit_rows(raw_conn, port) as rows:
        assert rows is None
    with lifecycle_read_operation(ledger.owner) as conn:
        with audit_rows(conn, _EvidenceReadPort(port)) as rows:
            assert rows is None  # No caller-owned BEGIN is invented.
        conn.execute("BEGIN")
        try:
            with audit_rows(conn, port) as rows:
                assert rows is None  # Old default port gains no callback restrictions.
        finally:
            conn.rollback()
    create(ledger, port, daily)
    observer = TracePort(port)
    protected = _EvidenceReadPort(observer)
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        try:
            expected = _audit_evidence_connection(conn, protected, ledger.binding)
            expected_trace = tuple(observer.trace)
            observer.trace.clear()
            # This changes only the private optimization budget, never a guard.
            monkeypatch.setattr("finance_core.lifecycle_v2._audit_rows_v1._MAX_BYTES", 1)
            with audit_rows(conn, protected) as rows:
                assert rows is None and observer.trace == []
            assert audit_evidence_connection(conn, protected, ledger.binding) == expected
            assert tuple(observer.trace) == expected_trace
        finally:
            conn.rollback()


@pytest.mark.parametrize("mode", ["_read", "_write"])
def test_scope_full_exit_admission_original_mode_and_invalidation(live, mode):
    ledger, port, daily, _, _ = setup(live)
    admissions = []

    def observe(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "_require_session":
            parent = frame.f_back
            if parent.f_code.co_name == "_submit_admission":
                grandparent = parent.f_back
                if grandparent.f_code.co_name in {"audit_rows", "finish"}:
                    admissions.append((grandparent.f_code.co_name, frame.f_locals["write"]))

    with getattr(daily, mode)() as (service, conn):
        old_profile = sys.getprofile()
        sys.setprofile(observe)
        try:
            with audit_rows(conn, service.port) as rows:
                assert rows is not None and rows.table(conn, "facts") == ()
        finally:
            sys.setprofile(old_profile)
        assert admissions == [("audit_rows", mode == "_write"), ("finish", mode == "_write")]
        with pytest.raises(LifecycleError, match="Expired"):
            rows.table(conn, "facts")
        with pytest.raises(RuntimeError, match="pure failure"):
            with audit_rows(conn, service.port) as failed:
                raise RuntimeError("pure failure")
        with pytest.raises(LifecycleError, match="Expired"):
            failed.table(conn, "facts")
        # A different actual connection cannot borrow the view, and the refusal
        # permanently invalidates it without any transaction or financial writes.
        with (
            pytest.raises(LifecycleError, match="Expired"),
            audit_rows(conn, service.port) as rejected,
        ):
            with sqlite3.connect(":memory:") as other:
                with pytest.raises(LifecycleError, match="Expired"):
                    rejected.table(other, "facts")
            with pytest.raises(LifecycleError, match="Expired"):
                rejected.table(conn, "facts")
            # Scope exit must refuse a poisoned lifetime as well.
