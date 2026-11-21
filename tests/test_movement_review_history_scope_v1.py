"""Review history optimization on a real synthetic admission and writer."""

import pytest

from finance_core.instance_admission import acquire_production_owner
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LedgerService, LifecycleError
from tests.test_daily_movements_lifecycle_v1 import fields, prepare
from tests.test_single_host_runtime_v1 import _admit
from tests.test_single_host_runtime_v1 import runtime_config as runtime_config


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

    def source(conn, identity):
        def authenticate():
            conn.execute("SELECT 1").fetchone()
            namespaces.append(identity)

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
    assert reader.collect().balances[0].balance_amount == "1000.00"
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
