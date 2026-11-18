"""One-audit immutable inventory, original prefix inputs and real SQL parity."""

import json
import sqlite3
import sys
from contextlib import closing
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
from finance_core.lifecycle_v2.daily_v1._impacts_v1 import (
    account_inventory,
    map_owner,
    opening_inventory,
)
from finance_core.lifecycle_v2.daily_v1._prefix_v1 import validate_order
from finance_core.lifecycle_v2.daily_v1.accounts import (
    _EvidenceReadPort,
    _require_prefix,
    _state_at,
    closure,
    registered_values,
    verify_fact,
)
from finance_core.lifecycle_v2.daily_v1.contracts import decode_expected
from finance_core.lifecycle_v2.reader import _audit_evidence_connection, audit_evidence_connection
from tests.daily_account_support_v1 import command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, create, setup
from tests.test_daily_movements_lifecycle_v1 import fields as movement_fields
from tests.test_daily_movements_lifecycle_v1 import movement
from tests.test_daily_opening_changes_v1 import (
    complete_dependencies,
    finish,
    opening_command,
    opening_proposal,
    prepare_opening,
)
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
    def __init__(self, port, *, changed_repeat=False, raw_conn=None):
        self.port = port
        self.changed_repeat = changed_repeat
        self.raw_conn = raw_conn
        self.trace = []
        self.tokens = []
        self.cursors = []

    def __getattr__(self, name):
        def call(conn, identity, expected=None):
            witness = _witness_for(conn)
            if self.raw_conn is not None:
                assert conn is self.raw_conn and witness is None
            else:
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
    for kind, fields, identity, reason in (
        ("opening_correct", {"amount": "-300", "as_of": unknown_time()}, "correct", "wrong amount"),
        ("opening_void", {}, "void", "wrong opening"),
        ("opening_set", {"amount": "0", "as_of": unknown_time()}, "new-opening", None),
    ):
        view = reader.get_account(first.account_ref.account_id)
        change(port, daily, view, kind, fields, identity, reason)
    observer = TracePort(port)
    protected = _EvidenceReadPort(observer)
    fact_calls, confirm_prefixes, fact_closures, prior_opening_versions = [], [], [], []

    def observe(frame, event, arg):
        if frame.f_code is verify_fact.__code__:
            if event == "call":
                actual_rows = frame.f_locals["_rows"]
                actual_conn = frame.f_locals["conn"]
                fact = frame.f_locals["fact"]
                if actual_rows is not None:
                    assert actual_rows._active and actual_rows._conn is actual_conn
                fact_calls.append((actual_rows, actual_conn, fact["version"]))
            elif event == "return" and "old" in frame.f_locals:
                prior_opening_versions.append(frame.f_locals["fact"]["version"])
        elif event == "call" and frame.f_back.f_code is verify_fact.__code__:
            if frame.f_code is _require_prefix.__code__:
                confirm_prefixes.append((frame.f_locals["stage"], frame.f_locals["_rows"]))
            elif frame.f_code is closure.__code__:
                fact_closures.append(frame.f_locals["_rows"])

    def observed(call):
        for observations in (fact_calls, confirm_prefixes, fact_closures, prior_opening_versions):
            observations.clear()
        previous_profile = sys.getprofile()
        sys.setprofile(observe)
        try:
            return call()
        finally:
            sys.setprofile(previous_profile)

    def assert_fact_scope(expected_rows, expected_conn):
        assert len(fact_calls) == 5
        assert all(rows is expected_rows and conn is expected_conn for rows, conn, _ in fact_calls)
        assert [version for _, _, version in fact_calls] == [1, 2, 3, 4, 5]
        assert confirm_prefixes == [("confirm", expected_rows)] * 5
        assert len(fact_closures) == 4 and all(rows is expected_rows for rows in fact_closures)
        assert prior_opening_versions == [5]

    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        try:
            before = _logical_database_digest(conn), conn.total_changes
            sql = observed(lambda: _audit_evidence_connection(conn, protected, ledger.binding))
            assert_fact_scope(None, conn)
            sql_trace = tuple(observer.trace)
            observer.trace.clear()
            with audit_rows(conn, protected) as rows:
                assert rows is not None
                indexed = observed(
                    lambda: _audit_evidence_connection(conn, protected, ledger.binding, _rows=rows)
                )
                assert_fact_scope(rows, conn)
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
            assert (
                observed(lambda: audit_evidence_connection(conn, protected, ledger.binding)) == sql
            )
            public_rows = fact_calls[0][0]
            assert public_rows is not None and public_rows is not rows
            assert_fact_scope(public_rows, conn)
            with pytest.raises(LifecycleError, match="Expired"):
                public_rows.table(conn, "facts")
            assert tuple(observer.trace) == sql_trace
            assert (_logical_database_digest(conn), conn.total_changes) == before
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


def test_movement_opening_prefixes_and_independent_copy_match_original_sql(
    live, tmp_path, monkeypatch
):
    ledger, port, daily, movements, reader, opening, source, initial, account, _ = prepare_opening(
        live
    )
    _, income_card, income = movement(
        ledger, port, movements, account, "income", movement_fields(account, "200", "income")
    )
    _, _, expense = movement(ledger, port, movements, account, "expense")
    completed = complete_dependencies(
        ledger,
        port,
        opening,
        source,
        initial,
        reader,
        account,
        "opening",
        opening_command(),
        {"expense": "excluded", "income": "included"},
    )
    opening_card, owner = finish(opening, completed, "opening")
    port.add_source("rename", "Rename bank to Renamed")
    port.commands["rename"] = command("account_rename", {"name": "Renamed"})
    rename_proposal = daily.propose_account_change(
        account,
        "rename",
        reader.get_account(account).bundle_ref,
        "propose-rename",
        reply_evidence_id="rename",
        command_kind="account_rename",
    )
    _, renamed = finish(daily, rename_proposal, "rename")
    _, void_card, voided = movement(
        ledger,
        port,
        movements,
        account,
        "void-income",
        {},
        reader.detail(income.receipt.record_id),
        "movement_void",
    )
    opening_proposal(
        ledger,
        port,
        opening,
        source,
        rename_proposal,
        reader.get_account(account),
        "pending",
        opening_command(amount="850"),
    )
    assert void_card.material["format"] == "finance-daily-movement-review-v2"
    assert void_card.material["account_impact_changes"][0]["balance_delta"] == "0.00"
    result = reader.collect_with_effects_v1()
    assert result.collection.pending_count == 1
    assert result.collection.balances[0].balance_amount == "891.00"
    assert result.collection.income_by_currency == ()
    assert result.collection.expense_by_currency == (("MYR", "9.00"),)
    assert result.collection.fact_version_count == 6

    observer = TracePort(port)
    protected = _EvidenceReadPort(observer)
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        try:
            before = _logical_database_digest(conn), conn.total_changes
            reference = _audit_evidence_connection(conn, protected, ledger.binding)
            trace = tuple(observer.trace)
            observer.trace.clear()
            assert {
                "opening_instruction_link_v1",
                "opening_proposal_v2",
                "movement_instruction_link",
                "movement_proposal",
                "action",
                "delivery",
            } <= {call[0] for call in trace}
            with audit_rows(conn, protected) as rows:
                assert rows is not None
                assert (
                    _audit_evidence_connection(conn, protected, ledger.binding, _rows=rows)
                    == reference
                )
                assert tuple(observer.trace) == trace
                # A commit is excluded at its own cutoff, included at the next sequence.
                for cut, count, bundle_version in (
                    (expense.receipt.commit_seq, 1, 1),
                    (owner.receipt.commit_seq, 2, 1),
                    (renamed.receipt.commit_seq, 2, 2),
                    (voided.receipt.commit_seq, 2, 3),
                    (voided.receipt.commit_seq + 1, 1, 3),
                    (None, 1, 3),
                ):
                    dependencies = opening_inventory(conn, account, cut, _rows=rows)
                    assert canonical(dependencies) == canonical(
                        opening_inventory(conn, account, cut)
                    )
                    accounts = account_inventory(conn, cut, _rows=rows)
                    assert canonical(accounts) == canonical(account_inventory(conn, cut))
                    assert len(dependencies) == count
                    assert accounts[0]["fact_ref"]["version"] == bundle_version
                    values = accounts[0]["values"]
                    mapped = map_owner(conn, values, cut, _rows=rows)
                    assert mapped == map_owner(conn, values, cut)
                    if bundle_version > 1:
                        assert mapped["version"] == owner.receipt.version
                        assert mapped["fact_sha256"] == owner.receipt.fact_sha256
                    else:
                        assert mapped is None
                    # Mutating a returned DTO cannot affect a warm projection.
                    accounts[0]["values"]["account"]["name"] = "changed"
                    assert account_inventory(conn, cut, _rows=rows)[0] != accounts[0]
                    if mapped is not None:
                        with pytest.raises(LifecycleError, match="carrier differs from prefix"):
                            map_owner(conn, values, cut, _rows=rows)
            assert (_logical_database_digest(conn), conn.total_changes) == before
            with pytest.raises(LifecycleError, match="Expired"):
                account_inventory(conn, _rows=rows)

            # A changed second result for the same proposal must still be
            # observed and rejected even with warmed prefix data.
            original = port.opening_proposal_v2
            failed = []
            for indexed in (False, True):
                seen = []

                def changed(conn, identity, expected):
                    actual = original(conn, identity, expected)
                    if identity == "opening":
                        seen.append(identity)
                        if len(seen) == 2:
                            return replace(actual, raw_proposal=actual.raw_proposal + " ")
                    return actual

                observer = TracePort(port)
                protected = _EvidenceReadPort(observer)
                with (
                    monkeypatch.context() as patch,
                    pytest.raises(LifecycleError, match="Authenticated opening proposal differs"),
                ):
                    patch.setattr(port, "opening_proposal_v2", changed)
                    if indexed:
                        with audit_rows(conn, protected) as rows:
                            assert rows is not None
                            _audit_evidence_connection(conn, protected, ledger.binding, _rows=rows)
                    else:
                        _audit_evidence_connection(conn, protected, ledger.binding)
                assert seen == ["opening", "opening"]
                failed.append(observer.trace)
            assert failed[0] == failed[1]
            assert (_logical_database_digest(conn), conn.total_changes) == before
        finally:
            conn.rollback()

    with monkeypatch.context() as patch:
        patch.setattr("finance_core.lifecycle_v2._audit_rows_v1._MAX_BYTES", 1)
        assert reader.collect_with_effects_v1() == result
        assert (
            opening.lookup_commit_receipt(
                "confirm-opening",
                review_id=opening_card.review_id,
                action_evidence_id="action-opening",
            )
            == owner
        )
        assert (
            movements.lookup_commit_receipt(
                "confirm-income",
                review_id=income_card.review_id,
                action_evidence_id="action-income",
            )
            == income
        )

    path = tmp_path / "daily-copy.sqlite"
    cut = ledger.seal_snapshot(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA query_only=ON")
        observer = TracePort(port, raw_conn=copy)
        with audit_rows(copy, _EvidenceReadPort(observer)) as rows:
            assert rows is None
        assert audit_evidence_connection(copy, observer, ledger.binding) == reference
        assert [c[:-1] for c in observer.trace] == [c[:-1] for c in trace]
        assert all(c[-1] == id(copy) for c in observer.trace)
        assert (
            _logical_database_digest(copy)
            == cut.logical_database_sha256
            == result.read_identity.logical_database_sha256
        )
        assert cut.commit_seq == result.read_identity.event_seq
        assert (
            canonical(opening_inventory(copy, account)[0]["resolved_effect"])
            == result.effects[0].material_json
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
