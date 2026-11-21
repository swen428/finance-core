"""Actual opt-in callback SQL/transaction/cursor capability qualification."""

import importlib
import sqlite3
from contextvars import copy_context

import pytest

from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LedgerService, LifecycleError
from tests.test_daily_movements_lifecycle_v1 import fields, prepare
from tests.test_lifecycle_v2 import live as live
from tests.test_single_host_runtime_v1 import _admit
from tests.test_single_host_runtime_v1 import runtime_config as runtime_config


def scope(conn):
    return importlib.import_module(
        "finance_core.instance_admission.sessions"
    )._evidence_read_callback(conn)


def test_actual_connection_select_close_and_old_writer_remain_usable(live):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = id(conn)
        with scope(conn):
            cursor = conn.execute("SELECT 7 AS value")
            assert isinstance(conn, sqlite3.Connection) and id(cursor.connection) == identity
            assert cursor.fetchone()["value"] == 7
            cursor.close()
        assert conn.in_transaction
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        conn.commit()


@pytest.mark.parametrize(
    "operation",
    ["commit", "rollback", "close", "enter", "exit", "isolation", "autocommit", "interrupt"],
)
def test_lifecycle_methods_refuse_before_ending_outer_transaction(live, operation):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                if operation == "enter":
                    conn.__enter__()
                elif operation == "exit":
                    conn.__exit__(None, None, None)
                elif operation == "isolation":
                    conn.isolation_level = ""
                elif operation == "autocommit":
                    conn.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
                else:
                    getattr(conn, operation)()
        assert conn.in_transaction
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        conn.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN IMMEDIATE",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT callback_sp",
        "RELEASE callback_sp",
        "ROLLBACK TO callback_sp",
        "PRAGMA defer_foreign_keys=ON",
        "PRAGMA foreign_keys=OFF",
        "CREATE TABLE forbidden_callback(value TEXT)",
        "DELETE FROM l2_sources",
        "UPDATE l2_sources SET raw_input='changed'",
        "INSERT INTO participants(public_id,display_name) VALUES('forbidden','forbidden')",
        "INSERT INTO participants(public_id,display_name) "
        "VALUES('forbidden','forbidden') RETURNING public_id",
        "UPDATE participants SET display_name='forbidden'",
        "DELETE FROM participants",
        "CREATE TRIGGER callback_trigger AFTER INSERT ON participants "
        "BEGIN UPDATE participants SET display_name='forbidden'; END",
        "PRAGMA user_version=1",
    ],
)
def test_sql_control_and_mutation_are_denied_and_sticky(live, sql):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                try:
                    conn.execute(sql)
                except (sqlite3.DatabaseError, InstanceAdmissionError):
                    pass
        assert conn.in_transaction
        conn.rollback()


def test_nested_scope_cannot_clear_swallowed_inner_violation(live):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                try:
                    with scope(conn):
                        try:
                            conn.commit()
                        except InstanceAdmissionError:
                            pass
                except InstanceAdmissionError:
                    pass
                assert conn.execute("SELECT 2").fetchone()[0] == 2
        conn.rollback()


def test_scope_cursors_expire_and_preexisting_cursors_cannot_advance(live):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT 1")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                old.fetchone()
        assert old.fetchone()[0] == 1
        with scope(conn):
            escaped = conn.execute("SELECT 2")
            assert escaped.fetchone()[0] == 2
        with pytest.raises(InstanceAdmissionError):
            escaped.execute("SELECT 3")
        old.close()
        conn.rollback()


@pytest.mark.parametrize(
    "surface", ["fetchone", "fetchmany", "fetchall", "iter", "next", "execute", "close"]
)
def test_prepared_pending_write_cursor_cannot_step_reset_or_finalize(live, surface):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "INSERT INTO participants(public_id,display_name) "
            "VALUES('pending','pending') RETURNING public_id"
        )
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                if surface == "iter":
                    iter(pending)
                elif surface == "next":
                    next(pending)
                elif surface == "execute":
                    pending.execute("SELECT 1")
                else:
                    getattr(pending, surface)()
        assert pending.fetchone()[0] == "pending"
        pending.close()
        conn.rollback()


def test_warmed_identical_write_and_transaction_sql_are_reauthorized(live):
    ledger, _, _ = live
    sql = "INSERT INTO participants(public_id,display_name) VALUES(?,?)"
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, ("normal", "normal"))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                try:
                    conn.cursor().execute(sql, ("forbidden", "forbidden"))
                except sqlite3.DatabaseError:
                    pass
        assert (
            conn.execute(
                "SELECT count(*) FROM participants WHERE public_id='forbidden'"
            ).fetchone()[0]
            == 0
        )
        conn.rollback()


@pytest.mark.parametrize(
    "port_name", ["source", "provider", "daily_proposal", "delivery", "action"]
)
def test_all_five_real_historical_ports_refuse_swallowed_insert_commit(live, port_name):
    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from tests.daily_account_support_v1 import command
    from tests.test_daily_accounts_lifecycle_v1 import create, setup
    from tests.test_lifecycle_v2 import TEXT, response

    ledger, port, daily, reader, _ = setup(live)
    if port_name == "provider":
        port.add_source("native", TEXT)
        port.native_raw["native-observation"] = response(TEXT)
        source = ledger.capture("native", "native-capture")
        proposal = ledger.propose(source.source_id, "native-observation", 0, "native-propose")
        review = ledger.prepare_review(proposal.proposal_id, proposal.version)
        ledger.record_delivery(review.review_id, "native-delivery")
        ledger.confirm(review.review_id, "native-action", "native-confirm")
    create(ledger, port, daily)
    port.add_source("source2", "Create another MYR account")
    source = ledger.capture("source2", "capture2")
    port.commands["second-observation"] = command()
    original = getattr(port, port_name)
    seen = []

    def hostile(conn, *args):
        seen.append(id(conn))
        for operation in (
            lambda: conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES('callback','callback')"
            ),
            conn.commit,
        ):
            try:
                operation()
            except (sqlite3.DatabaseError, InstanceAdmissionError):
                pass
        return original(conn, *args)

    setattr(port, port_name, hostile)
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        daily.propose_account_create(source.source_id, "second-observation", 0, "second-propose")
    assert len(seen) == 1
    setattr(port, port_name, original)
    # This opens a fresh issued connection after the failed operation closed.
    with lifecycle_read_operation(ledger.owner) as fresh:
        assert (
            fresh.execute(
                "SELECT count(*) FROM participants WHERE public_id='callback'"
            ).fetchone()[0]
            == 0
        )
        assert (
            fresh.execute(
                "SELECT count(*) FROM l2_proposals WHERE source_id=?", (source.source_id,)
            ).fetchone()[0]
            == 0
        )
    assert len(reader.collect_accounts().accounts) == 1


def test_method_resolution_and_scalar_materialization_are_inside_scope(live):
    from dataclasses import replace

    from finance_core.lifecycle_v2 import LifecycleError
    from finance_core.lifecycle_v2.daily_v1.accounts import _EvidenceReadPort

    ledger, port, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")

        class Dynamic:
            def __getattribute__(self, name):
                conn.commit()
                return getattr(port, name)

        with pytest.raises(InstanceAdmissionError):
            _EvidenceReadPort(Dynamic()).source(conn, "source1")

        class StringSubclass(str):
            pass

        port.sources["source1"] = replace(port.sources["source1"], raw_input=StringSubclass("text"))
        with pytest.raises(LifecycleError, match="exact scalar"):
            _EvidenceReadPort(port).source(conn, "source1")
        conn.rollback()


def _uncertain_callback_cursor_worker(tmp_path, sender):
    import os

    from instance_admission_support_v1 import synthetic_config

    from finance_core.instance_admission import (
        ProductionCloseUncertain,
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
        sessions,
    )

    cfg = synthetic_config(tmp_path, pytest.MonkeyPatch())
    prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(cfg)
    operation = sessions.lifecycle_read_operation(owner)
    conn = operation.__enter__()
    calls = []
    escaped = None

    def fail_close(cursor):
        calls.append(cursor)
        raise sqlite3.OperationalError("synthetic actual cursor close uncertainty")

    with pytest.raises(ProductionCloseUncertain):
        with scope(conn):
            escaped = conn.execute("SELECT 1")
            assert escaped.fetchone()[0] == 1
            sessions._close_cursor_handle = fail_close
    with pytest.raises(ProductionCloseUncertain):
        operation.__exit__(None, None, None)
    with pytest.raises(InstanceAdmissionError):
        owner.close()
    with pytest.raises(InstanceAdmissionError):
        cfg.close()
    witness = sessions._witness_for(conn)
    sender.send(
        (
            len(calls),
            owner._uncertain,
            witness.evidence_callback_token is None,
            any(cursor is escaped for cursor in witness.retained_cursors),
        )
    )
    sender.close()
    os._exit(0)


def test_callback_cursor_actual_close_unknown_retains_owner_without_retry(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_uncertain_callback_cursor_worker, args=(tmp_path, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(30), "callback close worker did not report"
        assert receiver.recv() == (1, True, True, True)
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        receiver.close()


@pytest.mark.parametrize("entry", ["full_audit", "snapshot"])
def test_generic_daily_audit_and_snapshot_source_activate_production_scope(live, tmp_path, entry):
    from tests.test_daily_accounts_lifecycle_v1 import create, setup

    ledger, port, daily, _, _ = setup(live)
    create(ledger, port, daily)
    original = port.source

    def swallowed_commit(conn, identity):
        try:
            conn.commit()
        except InstanceAdmissionError:
            pass
        return original(conn, identity)

    port.source = swallowed_commit
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        if entry == "full_audit":
            ledger.reader().full_audit()
        else:
            ledger.seal_snapshot(tmp_path / "blocked.sqlite3")
    port.source = original
    assert ledger.reader().full_audit()["fact_version_count"] == 1


def test_snapshot_copy_uses_independent_raw_readonly_boundary(live, tmp_path):
    from finance_core.instance_admission.sessions import _witness_for
    from tests.test_daily_accounts_lifecycle_v1 import create, setup

    ledger, port, daily, _, _ = setup(live)
    create(ledger, port, daily)
    original = port.source
    observed = []

    def inspect_boundary(conn, identity):
        witness = _witness_for(conn)
        observed.append((id(conn), witness is not None))
        if witness is not None:
            assert witness.evidence_callback_token is not None
        else:
            assert type(conn) is sqlite3.Connection
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO participants(public_id,display_name) VALUES('copy','copy')"
                )
        return original(conn, identity)

    port.source = inspect_boundary
    cut = ledger.seal_snapshot(tmp_path / "independent.sqlite3")
    assert len(cut.facts) == 1
    assert {is_production for _, is_production in observed} == {False, True}
    assert {identity for identity, production in observed if production}.isdisjoint(
        identity for identity, production in observed if not production
    )


@pytest.mark.parametrize(
    "surface", ["fetchone", "fetchmany", "fetchall", "iter", "next", "execute", "close"]
)
def test_escaped_callback_cursor_is_expired_after_scope(live, surface):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with scope(conn):
            escaped = conn.execute("SELECT 1 UNION ALL SELECT 2")
            assert escaped.fetchone()[0] == 1
        with pytest.raises(InstanceAdmissionError, match="Expired evidence"):
            if surface == "iter":
                iter(escaped)
            elif surface == "next":
                next(escaped)
            elif surface == "execute":
                escaped.execute(
                    "INSERT INTO participants(public_id,display_name) VALUES('escape','escape')"
                )
            else:
                getattr(escaped, surface)()
        conn.rollback()


@pytest.mark.parametrize(
    "configuration",
    ["row_factory", "text_factory", "arraysize", "cursor_row_factory", "cursor_factory"],
)
def test_callback_configuration_rejections_are_sticky(live, configuration):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError, match="read-only scope"):
            with scope(conn):
                try:
                    if configuration == "row_factory":
                        conn.row_factory = sqlite3.Row
                    elif configuration == "text_factory":
                        conn.text_factory = str
                    elif configuration == "arraysize":
                        conn.cursor().arraysize = 2
                    elif configuration == "cursor_row_factory":
                        conn.cursor().row_factory = sqlite3.Row
                    else:
                        conn.cursor(sqlite3.Cursor)
                except InstanceAdmissionError:
                    pass
        conn.rollback()


def test_expected_nested_material_cannot_be_changed_by_daily_provider(live):
    from finance_core.lifecycle_v2 import LifecycleError
    from tests.daily_account_support_v1 import command
    from tests.test_daily_accounts_lifecycle_v1 import setup

    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    original = port.daily_proposal

    def change_expected(conn, identity, expected):
        expected.instruction["raw_input"] = "altered instruction"
        return original(conn, identity, expected)

    port.daily_proposal = change_expected
    with pytest.raises(LifecycleError, match="changed its expected request"):
        daily.propose_account_create(source.source_id, "obs", 0, "propose")


@pytest.mark.parametrize("sql", ["COMMIT", "ROLLBACK", "SAVEPOINT callback_sp"])
def test_cursor_execute_transaction_control_is_also_preempted(live, sql):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InstanceAdmissionError):
            with scope(conn):
                try:
                    conn.cursor().execute(sql)
                except sqlite3.DatabaseError:
                    pass
        assert conn.in_transaction
        conn.rollback()


def test_callback_exception_restores_original_writer_cleanup_permissions(live):
    ledger, _, _ = live
    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="external evidence failed"):
            with scope(conn):
                assert conn.execute("SELECT 1").fetchone()[0] == 1
                raise RuntimeError("external evidence failed")
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO participants(public_id,display_name) VALUES('normal','normal')")
        conn.commit()


@pytest.fixture
def pending_review(runtime_config):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        ledger = LedgerService(owner, object(), binding, runtime_admission=admission)
        ledger, port, _, api, reader, account = prepare((ledger, None, config), currency="SGD")
        port.add_source("expense", "excluded; coffee")
        port.commands["expense"] = {
            "format": "finance-daily-movement-command-v1",
            "command_kind": "movement_create",
            "fields": fields(account, amount="8", original="8", currency="SGD"),
            "reason_span": None,
        }
        source = ledger.capture("expense", "expense-capture")
        port.remember_movement_source(source, "expense")
        port.register_movement_instruction(binding, source.source_id, "expense")
        proposal = api.propose_movement_create(source.source_id, "expense", 0, "expense-propose")
        yield ledger, port, api, reader, proposal


def test_review_history_shares_namespaces_and_keeps_fresh_target(pending_review, monkeypatch):
    from finance_core.lifecycle_v2.daily_v1 import review_history_scope_v1 as scopes

    ledger, port, api, reader, proposal = pending_review
    original_source = port.source
    calls = []
    namespaces = []
    retained_contexts = []

    def source(conn, identity):
        def authenticate():
            conn.execute("SELECT 1").fetchone()
            namespaces.append(identity)
            if not retained_contexts:
                retained_contexts.append(copy_context())

        shared = scopes.authenticate_movement_review_history_namespaces_v1(conn, port, authenticate)
        if not shared:
            authenticate()
        calls.append((identity, shared))
        return original_source(conn, identity)

    monkeypatch.setattr(port, "source", source)
    review = api.prepare_review(proposal.proposal_id, proposal.version)
    assert len(calls) > 3 and all(shared for _, shared in calls[:-2])
    assert calls[-2:] == [("expense", False), ("expense", False)]
    assert namespaces == [calls[0][0], "expense", "expense"]
    assert review.material["account_impact_changes"][0]["balance_delta"] == "-8.00"
    assert review.delivery_evidence_id is None
    with lifecycle_read_operation(ledger.owner) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM l2_reviews WHERE proposal_id=?", (proposal.proposal_id,)
            ).fetchone()[0]
            == 1
        )
    calls.clear()
    assert retained_contexts[0].run(reader.collect).balances[0].balance_amount == "1000.00"
    assert calls and all(not shared for _, shared in calls)


def test_caught_history_authentication_failure_aborts_review(pending_review, monkeypatch):
    from finance_core.lifecycle_v2.daily_v1 import review_history_scope_v1 as scopes

    ledger, port, api, _, proposal = pending_review
    original_source = port.source
    attempts = 0
    caught = 0

    def source(conn, identity):
        nonlocal caught

        def authenticate():
            nonlocal attempts
            attempts += 1
            raise ValueError("synthetic retained namespace evidence unavailable")

        try:
            scopes.authenticate_movement_review_history_namespaces_v1(conn, port, authenticate)
        except (ValueError, LifecycleError):
            caught += 1
        return original_source(conn, identity)

    with monkeypatch.context() as patch:
        patch.setattr(port, "source", source)
        with pytest.raises(LifecycleError, match="Review history namespace scope failed"):
            api.prepare_review(proposal.proposal_id, proposal.version)
    assert attempts == 1 and caught > 1
    with lifecycle_read_operation(ledger.owner) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM l2_reviews WHERE proposal_id=?", (proposal.proposal_id,)
            ).fetchone()[0]
            == 0
        )
    assert (
        api.prepare_review(proposal.proposal_id, proposal.version).proposal_id
        == proposal.proposal_id
    )
