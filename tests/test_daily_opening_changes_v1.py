"""Normal admitted opening dependency decisions with independent synthetic links."""

import pytest

from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.daily_v1 import (
    DailyAccountServiceV1,
    DailyLedgerReaderV1,
    DailyMovementServiceV1,
    DailyOpeningChangeServiceV1,
)
from tests.daily_account_support_v1 import NOW, OpeningPort, command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, create
from tests.test_daily_movements_lifecycle_v1 import fields, movement
from tests.test_lifecycle_v2 import live as live


def prepare_opening(live, opening="1000", currency="MYR", account_type="bank"):
    ledger, _, cfg = live
    port = OpeningPort()
    ledger.port = port
    ledger.clock = lambda: NOW
    daily = DailyAccountServiceV1(ledger)
    source, proposal, _, receipt = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Bank",
                "account_type": account_type,
                "currency": currency,
                "opening": {"amount": opening, "as_of": unknown_time()}
                if opening is not None
                else None,
            }
        ),
    )
    reader = DailyLedgerReaderV1(ledger)
    return (
        ledger,
        port,
        daily,
        DailyMovementServiceV1(ledger),
        reader,
        DailyOpeningChangeServiceV1(ledger),
        source,
        proposal,
        receipt.account_ref.account_id,
        cfg,
    )


def opening_command(kind="opening_correct", amount="900", boundary=None, answers=None):
    return {
        "format": "finance-daily-opening-command-v2",
        "command_kind": kind,
        "fields": {"amount": amount, "as_of": boundary or unknown_time()},
        "reason_span": [0, len("wrong opening")] if kind == "opening_correct" else None,
        "answers": answers or [],
    }


def opening_proposal(
    ledger,
    port,
    api,
    source,
    previous,
    account,
    identity,
    cmd,
    original=None,
    question=None,
    *,
    page_initial_dependencies=False,
):
    if identity not in port.sources:
        if original is not None:
            raise AssertionError(
                "Register the actual explicit row reply before its provider command"
            )
        port.add_source(
            identity,
            "wrong opening; set "
            + cmd["fields"].get("amount", "unspecified")
            + " "
            + account.currency
            + "; as-of "
            + canonical(cmd["fields"].get("as_of")),
        )
    port.register_opening(
        ledger.binding, source, account, previous, identity, original, cmd["command_kind"], question
    )
    port.commands[identity] = cmd
    return api.propose_opening_change_v2(
        account.account_id,
        identity,
        account.bundle_ref,
        "propose-" + identity,
        reply_evidence_id=identity,
        command_kind=cmd["command_kind"],
        opening_instruction_evidence_id=original,
        page_initial_dependencies=page_initial_dependencies,
    )


def row_reply(port, identity, *rows):
    """A synthetic actual user message, before any provider callback runs."""
    phrases = {"included": "included", "excluded": "not included", "unknown": "unknown"}
    raw = "; ".join(
        s["movement_fact_ref"]["record_id"] + ":" + s["impact_role"] + "=" + phrases[c]
        for s, c in rows
    )
    port.add_source(identity, raw)
    answers = []
    for selected, choice in rows:
        marker = selected["movement_fact_ref"]["record_id"] + ":" + selected["impact_role"] + "="
        start = raw.index(marker) + len(marker)
        answers.append(
            {
                "movement_fact_ref": selected["movement_fact_ref"],
                "impact_role": selected["impact_role"],
                "treatment": choice,
                "span": [start, start + len(phrases[choice])] if choice != "unknown" else None,
            }
        )
    return answers


def finish(api, proposal, identity):
    card = api.prepare_review(proposal.proposal_id, proposal.version)
    api.record_delivery(card.review_id, "delivery-" + identity)
    receipt = api.confirm(card.review_id, "action-" + identity, "confirm-" + identity)
    return card, receipt


def financial_rows(ledger):
    with lifecycle_read_operation(ledger.owner) as conn:
        return tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT record_id,version,values_json,fact_sha256 FROM l2_facts "
                "ORDER BY record_id,version"
            )
        )


def test_question_page_format_binds_all_five_hundred_subjects():
    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import (
        question_pages_v2,
        verify_question_pages_v2,
    )

    subjects = [
        {
            "movement_fact_ref": {"record_id": f"movement-{ordinal:03}"},
            "impact_role": "debit",
            "description": "测试消费 " * 32,
        }
        for ordinal in range(500)
    ]
    material = {"format": "finance-daily-opening-question-list-v1", "subjects": subjects}
    current = {
        **material,
        "proposal_ref": {"proposal_id": "synthetic-proposal"},
        "list_sha256": digest(material),
    }
    result = question_pages_v2(current)
    assert result["subject_count"] == 500 and len(result["pages"]) == 32
    assert [s for page in result["pages"] for s in page["subjects"]] == subjects
    for index, page in enumerate(result["pages"], 1):
        assert page["index"] == index
        assert page["page_sha256"] == digest({k: v for k, v in page.items() if k != "page_sha256"})
    assert result["page_sha256"] == [page["page_sha256"] for page in result["pages"]]
    assert result["manifest_sha256"] == digest(
        {key: value for key, value in result.items() if key not in {"manifest_sha256", "pages"}}
    )
    expected = {name: current[name] for name in ("proposal_ref", "list_sha256")}
    assert verify_question_pages_v2(result, **expected) == tuple(subjects)
    with pytest.raises(LifecycleError, match="complete original list|manifest differs"):
        verify_question_pages_v2({**result, "pages": result["pages"][:-1]}, **expected)
    with pytest.raises(LifecycleError, match="page identity differs"):
        verify_question_pages_v2({**result, "pages": list(reversed(result["pages"]))}, **expected)
    with pytest.raises(LifecycleError, match="manifest differs"):
        verify_question_pages_v2(result, **(expected | {"proposal_ref": {"proposal_id": "other"}}))


@pytest.mark.parametrize("paged", [False, True], ids=["scalar", "paged"])
def test_nonempty_correction_partial_answers_rename_and_resolved_void(live, request, paged):
    ledger, port, daily, movements, reader, api, source, initial, account, cfg = prepare_opening(
        live
    )
    _, income_card, income = movement(
        ledger, port, movements, account, "income", fields(account, "200", "income")
    )
    _, _, expense = movement(ledger, port, movements, account, "expense")
    originals = financial_rows(ledger)
    draft = opening_proposal(
        ledger,
        port,
        api,
        source,
        initial,
        reader.get_account(account),
        "opening",
        opening_command(),
        page_initial_dependencies=paged,
    )
    assert len(api.pending()[0]["values"]["missing_fields"]) == 2
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(draft.proposal_id, draft.version)
    questions = api.opening_questions_v1(draft.proposal_id)
    framed = api.opening_questions_v2(draft.proposal_id)
    assert framed["proposal_ref"] == questions["proposal_ref"]
    assert framed["list_sha256"] == questions["list_sha256"]
    assert framed["subject_count"] == len(questions["subjects"]) == 2
    assert [subject for page in framed["pages"] for subject in page["subjects"]] == (
        questions["subjects"]
    )
    by_kind = {s["kind"]: s for s in questions["subjects"]}
    q1 = port.deliver_opening_question(questions, "q1")
    income_answer = row_reply(port, "answer1", (by_kind["income"], "included"))
    first = opening_proposal(
        ledger,
        port,
        api,
        source,
        draft,
        reader.get_account(account),
        "answer1",
        opening_command(answers=income_answer),
        "opening",
        q1,
    )
    assert len(api.pending()[0]["values"]["missing_fields"]) == 1
    assert reader.collect().balances[0].balance_amount == "1191.00"
    from finance_core.instance_admission import acquire_production_owner
    from finance_core.lifecycle_v2 import LedgerService

    binding = ledger.binding
    ledger.owner.close()
    reopened = acquire_production_owner(cfg)
    request.addfinalizer(reopened.close)
    ledger = LedgerService(reopened, port, binding, clock=lambda: NOW)
    daily = DailyAccountServiceV1(ledger)
    movements = DailyMovementServiceV1(ledger)
    reader = DailyLedgerReaderV1(ledger)
    api = DailyOpeningChangeServiceV1(ledger)
    retained = api.pending()[0]["values"]
    assert len(retained["missing_fields"]) == 1
    assert (
        next(a for a in retained["answers"] if a["subject"]["kind"] == "income")["answer_ref"][
            "instruction_ref"
        ]["evidence_id"]
        == "answer1"
    )
    q2 = port.deliver_opening_question(api.opening_questions_v1(first.proposal_id), "q2")
    expense_answer = row_reply(port, "answer2", (by_kind["expense"], "excluded"))
    complete = opening_proposal(
        ledger,
        port,
        api,
        source,
        first,
        reader.get_account(account),
        "answer2",
        opening_command(answers=expense_answer),
        "opening",
        q2,
    )
    card, receipt = finish(api, complete, "opening")
    effects = card.material["opening_effects"]
    assert (effects["before_balance"], effects["after_balance"], effects["balance_delta"]) == (
        "1191.00",
        "891.00",
        "-300.00",
    )
    assert len(effects["rows"]) == 2 and not effects["statistics_changed"]
    result = reader.collect_with_effects_v1()
    assert result.read_identity is result.collection.read_identity
    assert result.collection.balances[0].balance_amount == "891.00"
    assert result.collection.balances[0].recorded_net_change == "191.00"
    assert result.collection.income_by_currency == (("MYR", "200.00"),)
    assert result.collection.expense_by_currency == (("MYR", "9.00"),)
    assert all(e.values["source"] == "map" for e in result.effects)
    assert all(
        e.values["map_owner_fact_ref"]["version"] == receipt.receipt.version for e in result.effects
    )
    now = {r[:2]: r[2:] for r in financial_rows(ledger)}
    assert all(now[r[:2]] == r[2:] for r in originals)
    assert len(now) == len(originals) + 1
    with lifecycle_read_operation(ledger.owner) as conn:
        assert tuple(
            conn.execute(
                "SELECT (SELECT count(*) FROM l2_facts),(SELECT count(*) FROM l2_decisions),"
                "(SELECT count(*) FROM l2_events WHERE kind='confirm')"
            ).fetchone()
        ) == (4, 4, 4)
    assert (
        movements.lookup_commit_receipt(
            "confirm-income", review_id=income_card.review_id, action_evidence_id="action-income"
        )
        == income
    )
    assert reader.source(source.source_id) == port.sources["source1"]
    assert api.confirm(card.review_id, "action-opening", "confirm-opening") == receipt
    old_map = reader.get_account(account).values["opening_dependency_map"]
    port.add_source("rename", "Rename this bank to Renamed")
    port.commands["rename"] = command("account_rename", {"name": "Renamed"})
    renamed = daily.propose_account_change(
        account,
        "rename",
        reader.get_account(account).bundle_ref,
        "propose-rename",
        reply_evidence_id="rename",
        command_kind="account_rename",
    )
    finish(daily, renamed, "rename")
    assert reader.get_account(account).values["opening_dependency_map"] == old_map
    assert all(
        e.values["map_owner_fact_ref"]["version"] == receipt.receipt.version
        for e in reader.collect_with_effects_v1().effects
    )
    # The included income is withdrawn with zero balance effect.
    target = reader.detail(income.receipt.record_id)
    _, void_card, _ = movement(
        ledger, port, movements, account, "void-income", {}, target, "movement_void"
    )
    assert void_card.material["format"] == "finance-daily-movement-review-v2"
    assert void_card.material["account_impact_changes"][0]["balance_delta"] == "0.00"
    assert reader.collect().balances[0].balance_amount == "891.00"
    after_void = reader.collect()
    assert after_void.income_by_currency == ()
    assert after_void.expense_by_currency == (("MYR", "9.00"),)
    assert after_void.balances[0].recorded_net_change == "-9.00"
    assert reader.detail(expense.receipt.record_id).values["coverage"]["treatment"] == "excluded"
    second = complete_dependencies(
        ledger,
        port,
        api,
        source,
        renamed,
        reader,
        account,
        "opening-second",
        opening_command(amount="850"),
        {"expense": "included"},
    )
    second_card = api.prepare_review(second.proposal_id, second.version)
    api.record_delivery(second_card.review_id, "delivery-opening-second")
    port.add_source("source2", "Create another MYR bank with opening 500")
    _, _, _, other = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Other",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "500", "as_of": unknown_time()},
            }
        ),
        "2",
    )
    movement(
        ledger,
        port,
        movements,
        other.account_ref.account_id,
        "unrelated-income",
        fields(other.account_ref.account_id, "50", "income"),
    )
    second_receipt = api.confirm(
        second_card.review_id, "action-opening-second", "confirm-opening-second"
    )
    balances = {b.account_id: b.balance_amount for b in reader.collect().balances}
    assert balances == {account: "850.00", other.account_ref.account_id: "550.00"}
    assert second_card.material["opening_effects"]["before_balance"] == "891.00"
    assert second_card.material["opening_effects"]["after_balance"] == "850.00"
    assert second_receipt.opening_ref.opening_version == receipt.opening_ref.opening_version + 1
    assert (
        api.lookup_commit_receipt(
            "confirm-opening", review_id=card.review_id, action_evidence_id="action-opening"
        )
        == receipt
    )
    assert (
        movements.lookup_commit_receipt(
            "confirm-income", review_id=income_card.review_id, action_evidence_id="action-income"
        )
        == income
    )


def complete_dependencies(
    ledger, port, api, source, previous, reader, account, identity, cmd, choices
):
    draft = opening_proposal(
        ledger, port, api, source, previous, reader.get_account(account), identity, cmd
    )
    material = api.opening_questions_v1(draft.proposal_id)
    question = port.deliver_opening_question(material, "question-" + identity)
    answers = row_reply(
        port, "reply-" + identity, *((s, choices[s["kind"]]) for s in material["subjects"])
    )
    reply_cmd = {**cmd, "answers": answers}
    complete = opening_proposal(
        ledger,
        port,
        api,
        source,
        draft,
        reader.get_account(account),
        "reply-" + identity,
        reply_cmd,
        identity,
        question,
    )
    return complete


def test_unknown_opening_set_strictly_later_restart_and_snapshot(live, tmp_path):
    import sqlite3

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.lifecycle_v2 import LedgerService
    from finance_core.lifecycle_v2.reader import audit_evidence_connection

    ledger, port, _, movements, reader, api, source, initial, account, cfg = prepare_opening(
        live, None
    )
    movement(ledger, port, movements, account, "earlier", fields(account, "20", "income"))
    later = fields(account)
    later["funds_time"] = {
        **unknown_time(),
        "precision": "instant",
        "timestamp": NOW + 10,
        "local_date": "2026-11-17",
    }
    movement(ledger, port, movements, account, "later", later)
    assert reader.collect().balances[0].balance_amount is None
    originals = financial_rows(ledger)
    boundary = {
        **unknown_time(),
        "precision": "instant",
        "timestamp": NOW,
        "local_date": "2026-11-17",
    }
    cmd = opening_command("opening_set", "100", boundary)
    draft = opening_proposal(
        ledger, port, api, source, initial, reader.get_account(account), "set-opening", cmd
    )
    assert len(api.pending()[0]["values"]["missing_fields"]) == 1
    question = port.deliver_opening_question(api.opening_questions_v1(draft.proposal_id), "set-q")
    chosen = next(s for s in question["subjects"] if s["kind"] == "income")
    later_subject = next(s for s in question["subjects"] if s["kind"] == "expense")
    contradiction = row_reply(port, "later-contradiction", (later_subject, "included"))
    with pytest.raises(LifecycleError, match="contradicts strictly later"):
        opening_proposal(
            ledger,
            port,
            api,
            source,
            draft,
            reader.get_account(account),
            "later-contradiction",
            {**cmd, "answers": contradiction},
            "set-opening",
            question,
        )
    assert financial_rows(ledger) == originals
    # A real owner close/reacquisition, with retained synthetic transport receipts.
    ledger.owner.close()
    with acquire_production_owner(cfg) as reopened:
        service = LedgerService(reopened, port, ledger.binding, clock=lambda: NOW)
        api = DailyOpeningChangeServiceV1(service)
        reader = DailyLedgerReaderV1(service)
        assert len(api.pending()[0]["values"]["missing_fields"]) == 1
        answers = row_reply(port, "set-answer", (chosen, "included"))
        complete = opening_proposal(
            service,
            port,
            api,
            source,
            draft,
            reader.get_account(account),
            "set-answer",
            {**cmd, "answers": answers},
            "set-opening",
            question,
        )
        card, receipt = finish(api, complete, "set")
        assert card.material["opening_effects"]["before_balance"] is None
        result = reader.collect_with_effects_v1()
        assert result.collection.balances[0].balance_amount == "91.00"
        assert {e.values["treatment"] for e in result.effects} == {"included", "excluded"}
        assert all(e.values["source"] == "map" for e in result.effects)
        current = {r[:2]: r[2:] for r in financial_rows(service)}
        assert all(current[r[:2]] == r[2:] for r in originals)
        cut = service.seal_snapshot(tmp_path / "opening.sqlite")
        with sqlite3.connect(
            (tmp_path / "opening.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
        ) as copy:
            copy.row_factory = sqlite3.Row
            copy.execute("PRAGMA query_only=ON")
            audited = audit_evidence_connection(copy, port, service.binding)
            assert audited["fact_version_count"] == result.collection.fact_version_count
            from finance_core.instance_admission.authority import _logical_database_digest
            from finance_core.lifecycle_v2.daily_v1._impacts_v1 import opening_inventory

            copy_seq = copy.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
            copy_facts = tuple(
                tuple(r)
                for r in copy.execute(
                    "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
                    "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) "
                    "ORDER BY record_id"
                )
            )
            assert copy_seq == cut.commit_seq == result.read_identity.event_seq
            assert copy_facts == cut.facts
            assert (
                _logical_database_digest(copy)
                == cut.logical_database_sha256
                == result.read_identity.logical_database_sha256
            )
            assert audited["event_count"] == copy_seq
            assert {
                d["fact_ref"]["record_id"]: d["resolved_effect"]
                for d in opening_inventory(copy, account)
            } == {e.values["movement_fact_ref"]["record_id"]: e.values for e in result.effects}
        assert (
            api.lookup_commit_receipt(
                "confirm-set", review_id=card.review_id, action_evidence_id="action-set"
            )
            == receipt
        )


def test_mapped_expense_correction_account_currency_change_and_void(live):
    ledger, port, daily, movements, reader, api, source, initial, account, _ = prepare_opening(
        live, "-300", currency="MYR", account_type="credit_card"
    )
    _, _, expense = movement(
        ledger,
        port,
        movements,
        account,
        "jpy",
        fields(account, "9", original="1000", currency="JPY"),
    )
    complete = complete_dependencies(
        ledger,
        port,
        api,
        source,
        initial,
        reader,
        account,
        "correct-opening",
        opening_command(amount="-400"),
        {"expense": "included"},
    )
    _, opening_receipt = finish(api, complete, "correct-opening")
    assert reader.collect().balances[0].balance_amount == "-400.00"
    port.add_source("source2", "Create SGD bank opening 500")
    _, _, _, b = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Other",
                "account_type": "bank",
                "currency": "SGD",
                "opening": {"amount": "500", "as_of": unknown_time()},
            }
        ),
        "2",
    )
    other = b.account_ref.account_id
    target = reader.detail(expense.receipt.record_id)
    _, corrected_card, corrected = movement(
        ledger,
        port,
        movements,
        other,
        "reassign",
        fields(other, "10", original="1000", currency="JPY"),
        target,
        "movement_correct",
    )
    assert {
        r["account_id"]: r["balance_delta"]
        for r in corrected_card.material["account_impact_changes"]
    } == {account: "0.00", other: "-10.00"}
    result = reader.collect_with_effects_v1()
    assert {b.account_id: b.balance_amount for b in result.collection.balances} == {
        account: "-400.00",
        other: "490.00",
    }
    assert result.collection.expense_by_currency == (("JPY", "1000"),)
    assert result.effects[0].values["source"] == "direct"
    assert result.effects[0].values["map_owner_fact_ref"] is None
    old_map = reader.get_account(account).values["opening_dependency_map"]
    assert old_map["entries"][0]["movement_fact_ref"]["version"] == 1
    _, void_card, _ = movement(
        ledger,
        port,
        movements,
        other,
        "void-corrected",
        {},
        reader.detail(corrected.receipt.record_id),
        "movement_void",
    )
    assert void_card.material["account_impact_changes"][0]["balance_delta"] == "10.00"
    assert reader.collect().expense_by_currency == ()
    assert opening_receipt.receipt.version == 2


def test_nonempty_void_refused_then_empty_void_new_id_and_no_baseline_set(live):
    ledger, port, daily, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    _, _, expense = movement(ledger, port, movements, account, "coffee")
    complete = complete_dependencies(
        ledger,
        port,
        api,
        source,
        initial,
        reader,
        account,
        "opening1",
        opening_command(),
        {"expense": "included"},
    )
    _, first = finish(api, complete, "opening1")
    with pytest.raises(LifecycleError, match="nonempty_opening_dependencies"):
        change(
            port,
            daily,
            reader.get_account(account),
            "opening_void",
            {},
            "refuse-void",
            "still has dependency",
        )
    _, void_card, _ = movement(
        ledger,
        port,
        movements,
        account,
        "void-coffee",
        {},
        reader.detail(expense.receipt.record_id),
        "movement_void",
    )
    assert void_card.material["account_impact_changes"][0]["balance_delta"] == "0.00"
    cleared = change(
        port,
        daily,
        reader.get_account(account),
        "opening_void",
        {},
        "clear-opening",
        "wrong baseline",
    )
    assert reader.get_account(account).values["opening_dependency_map"] is None
    movement(ledger, port, movements, account, "new-income", fields(account, "10", "income"))
    # The latest account proposal is the legitimate opening VOID proposal.
    with lifecycle_read_operation(ledger.owner) as conn:
        latest = conn.execute(
            "SELECT proposal_id,version,proposal_sha256 FROM l2_proposals WHERE source_id=? "
            "ORDER BY version DESC LIMIT 1",
            (source.source_id,),
        ).fetchone()
        from finance_core.lifecycle_v2.contracts import ProposalRef

        previous = ProposalRef(latest[0], source.source_id, latest[1], latest[2])
    complete = complete_dependencies(
        ledger,
        port,
        api,
        source,
        previous,
        reader,
        account,
        "opening2",
        opening_command("opening_set", "50"),
        {"income": "excluded"},
    )
    _, second = finish(api, complete, "opening2")
    assert second.opening_ref.opening_event_id != first.opening_ref.opening_event_id
    assert cleared.opening_ref.opening_event_id == first.opening_ref.opening_event_id
    assert reader.collect().balances[0].balance_amount == "60.00"


@pytest.mark.parametrize("same_account", [True, False], ids=["same-account", "other-account"])
def test_first_delivery_checks_current_opening_dependencies(live, same_account):
    from finance_core.lifecycle_v2 import VerifiedDelivery

    ledger, port, daily, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement_account = account
    if not same_account:
        port.add_source("source2", "Create another MYR cash account with opening 100")
        _, _, _, other = create(
            ledger,
            port,
            daily,
            command(
                fields={
                    "name": "Cash",
                    "account_type": "cash",
                    "currency": "MYR",
                    "opening": {"amount": "100", "as_of": unknown_time()},
                }
            ),
            suffix="2",
        )
        movement_account = other.account_ref.account_id

    movement(ledger, port, movements, account, "first-delivery-coffee")
    complete = complete_dependencies(
        ledger,
        port,
        api,
        source,
        initial,
        reader,
        account,
        "first-delivery-opening",
        opening_command(),
        {"expense": "included"},
    )
    card = api.prepare_review(complete.proposal_id, complete.version)
    movement(
        ledger,
        port,
        movements,
        movement_account,
        "first-delivery-income",
        fields(movement_account, "20", "income"),
    )
    port.deliveries["delivery-first-opening"] = VerifiedDelivery(
        "delivery-first-opening", card.review_id, card.review_sha256, "user", "chat"
    )

    def counts():
        with lifecycle_read_operation(ledger.owner) as conn:
            return tuple(
                conn.execute(
                    "SELECT (SELECT count(*) FROM l2_deliveries),"
                    "(SELECT count(*) FROM l2_events),"
                    "(SELECT count(*) FROM l2_decisions),"
                    "(SELECT count(*) FROM l2_facts)"
                ).fetchone()
            )

    before = counts()
    if same_account:
        with pytest.raises(LifecycleError, match="stale_opening_dependency_closure"):
            api.record_delivery(card.review_id, "delivery-first-opening")
        assert counts() == before
        assert api.prepare_review(complete.proposal_id, complete.version) == card
    else:
        delivered = api.record_delivery(card.review_id, "delivery-first-opening")
        assert delivered.delivery_evidence_id == "delivery-first-opening"
        assert counts() == (before[0] + 1, before[1] + 1, *before[2:])


def test_stale_closure_card_zero_append_and_fresh_card_required(live, monkeypatch):
    from finance_core.lifecycle_v2.daily_v1 import opening_changes_v1 as opening

    ledger, port, _, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement(ledger, port, movements, account, "coffee")
    complete = complete_dependencies(
        ledger,
        port,
        api,
        source,
        initial,
        reader,
        account,
        "opening-stale",
        opening_command(),
        {"expense": "included"},
    )
    card = api.prepare_review(complete.proposal_id, complete.version)
    delivered = api.record_delivery(card.review_id, "delivery-stale")
    movement(ledger, port, movements, account, "new-income", fields(account, "20", "income"))
    with lifecycle_read_operation(ledger.owner) as conn:
        review_seq, delivery_seq = (
            row[0]
            for row in conn.execute(
                "SELECT seq FROM l2_events WHERE entity_id=? AND kind IN ('review','delivery') "
                "ORDER BY seq",
                (card.review_id,),
            )
        )
        event_count = conn.execute("SELECT count(*) FROM l2_events").fetchone()[0]

    dependency_prefixes = set()
    require_dependencies = opening.require_dependencies

    def observe_dependencies(conn, expected, before_seq=None, *, _rows=None):
        if expected.reply_evidence_id == "reply-opening-stale":
            dependency_prefixes.add(before_seq)
        return require_dependencies(conn, expected, before_seq, _rows=_rows)

    with monkeypatch.context() as patch:
        patch.setattr(opening, "require_dependencies", observe_dependencies)
        assert ledger.reader().full_audit()["event_count"] == event_count
    assert dependency_prefixes == {review_seq, delivery_seq}
    assert api.record_delivery(card.review_id, "delivery-stale") == delivered
    assert ledger.reader().full_audit()["event_count"] == event_count

    before = financial_rows(ledger)
    with pytest.raises(LifecycleError, match="stale_opening_dependency_closure"):
        api.confirm(card.review_id, "action-stale", "confirm-stale")
    assert financial_rows(ledger) == before
    assert reader.collect().balances[0].balance_amount == "1011.00"
    # Fresh original instruction and complete current list replace the stale card.
    fresh = complete_dependencies(
        ledger,
        port,
        api,
        source,
        complete,
        reader,
        account,
        "opening-fresh",
        opening_command(amount="950"),
        {"income": "included", "expense": "excluded"},
    )
    fresh_card = api.prepare_review(fresh.proposal_id, fresh.version)
    with pytest.raises(LifecycleError):
        api.confirm(fresh_card.review_id, "action-without-delivery", "confirm-without-delivery")
    api.record_delivery(fresh_card.review_id, "delivery-fresh")
    with pytest.raises(LifecycleError, match="independent"):
        api.confirm(fresh_card.review_id, "reply-opening-fresh", "cannot-borrow-answer")
    receipt = api.confirm(fresh_card.review_id, "action-fresh", "confirm-fresh")
    assert reader.collect().balances[0].balance_amount == "941.00"
    assert (
        api.lookup_commit_receipt(
            "confirm-fresh", review_id=fresh_card.review_id, action_evidence_id="action-fresh"
        )
        == receipt
    )


def test_wrong_question_answer_and_negative_span_refused(live):
    ledger, port, _, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement(ledger, port, movements, account, "coffee")
    draft = opening_proposal(
        ledger,
        port,
        api,
        source,
        initial,
        reader.get_account(account),
        "opening-draft",
        opening_command(),
    )
    material = api.opening_questions_v1(draft.proposal_id)
    question = port.deliver_opening_question(material, "negative-q")
    chosen = question["subjects"][0]
    answers = row_reply(port, "negative-answer", (chosen, "excluded"))
    # An AI cannot slice `included` out of the genuine `not included` choice.
    bad = [
        {
            **answers[0],
            "treatment": "included",
            "span": [answers[0]["span"][0] + 4, answers[0]["span"][1]],
        }
    ]
    before = financial_rows(ledger)
    with pytest.raises(LifecycleError, match="complete choice|complete opening answer|row/span"):
        opening_proposal(
            ledger,
            port,
            api,
            source,
            draft,
            reader.get_account(account),
            "negative-answer",
            opening_command(answers=bad),
            "opening-draft",
            question,
        )
    assert financial_rows(ledger) == before
    # A genuinely delivered but differently worded 90-unit subject cannot
    # authorize treatment of the actual 9-unit row.
    import copy

    wrong = copy.deepcopy(material)
    wrong["subjects"][0]["account_amount"] = "90.00"
    wrong["list_sha256"] = digest({"format": material["format"], "subjects": wrong["subjects"]})
    wrong_question = port.deliver_opening_question(wrong, "wrong-q")
    answers = row_reply(port, "wrong-answer", (chosen, "included"))
    with pytest.raises(LifecycleError, match="actual complete question"):
        opening_proposal(
            ledger,
            port,
            api,
            source,
            draft,
            reader.get_account(account),
            "wrong-answer",
            opening_command(answers=answers),
            "opening-draft",
            wrong_question,
        )
    assert financial_rows(ledger) == before
    assert api.pending()[0]["values"]["missing_fields"]


@pytest.mark.parametrize("callback", ["opening_instruction_link_v1", "opening_proposal_v2"])
def test_new_callbacks_keep_original_issued_connection_readonly_scope(live, monkeypatch, callback):
    from finance_core.instance_admission import InstanceAdmissionError

    ledger, port, _, _, reader, api, source, initial, account, _ = prepare_opening(live)
    port.add_source("guarded-opening", "wrong opening; amount 900 MYR")
    port.register_opening(
        ledger.binding, source, reader.get_account(account), initial, "guarded-opening"
    )
    port.commands["guarded-opening"] = opening_command()
    original = getattr(port, callback)
    seen = []

    def guarded(conn, identity, expected):
        seen.append((id(conn), conn.in_transaction))
        try:
            conn.execute("CREATE TABLE opening_callback_write_forbidden(value)")
        except (InstanceAdmissionError, LifecycleError):
            pass
        return original(conn, identity, expected)

    monkeypatch.setattr(port, callback, guarded)
    before = financial_rows(ledger)
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        api.propose_opening_change_v2(
            account,
            "guarded-opening",
            reader.get_account(account).bundle_ref,
            "propose-guarded",
            reply_evidence_id="guarded-opening",
            command_kind="opening_correct",
        )
    assert financial_rows(ledger) == before
    assert seen and all(active for _, active in seen)


def test_v1_account_component_bytes_match_protected_baseline():
    from dataclasses import replace

    from finance_core.lifecycle_v2.daily_v1.contracts import compile_bundle
    from tests.test_daily_accounts_contracts_v1 import as_of, command, expected

    created = compile_bundle(
        command(
            fields={
                "name": "Daily",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "1000", "as_of": as_of()},
            }
        ),
        expected(),
    )
    renamed = compile_bundle(
        command("account_rename", {"name": "Renamed"}),
        replace(expected("account_rename", created), opening_event_id="opening-2"),
    )
    corrected = compile_bundle(
        command("opening_correct", {"amount": "900", "as_of": as_of()}, [30, 43]),
        expected("opening_correct", renamed),
    )
    # Captured by executing the original exact codec from protected B=74943c3.
    assert [digest(v) for v in (created, renamed, corrected)] == [
        "f624e877a1d4e6d13603639b4c02bfb712db2d20b2f329a588ae501fbdd65d84",
        "45cb0d12929c637788918236783771a50298c46e1054f078af7874a5ef89ba7c",
        "bbfd9cdeffde80e83484256d1d6142b65fcb32298dc404bda7b0115d729e9e9a",
    ]


def test_historical_opening_expected_cannot_borrow_another_instance():
    from types import SimpleNamespace

    from finance_core.lifecycle_v2 import RuntimeBinding
    from finance_core.lifecycle_v2.daily_v1.opening_changes_v1 import _compose

    binding = RuntimeBinding("actual-instance", "generation", "user", "chat")
    other = RuntimeBinding("other-instance", "generation", "user", "chat")
    with pytest.raises(LifecycleError, match="another instance"):
        _compose(
            SimpleNamespace(binding=binding),
            None,
            None,
            None,
            None,
            "opening_correct",
            None,
            None,
            binding=other,
        )


def test_opening_answer_composition_requires_the_predecessor_binding(live):
    from dataclasses import replace

    from finance_core.lifecycle_v2.daily_v1.opening_changes_v1 import _compose, _previous

    ledger, port, _, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement(ledger, port, movements, account, "binding-coffee")
    view = reader.get_account(account)
    draft = opening_proposal(
        ledger, port, api, source, initial, view, "binding-opening", opening_command()
    )
    question = port.deliver_opening_question(
        api.opening_questions_v1(draft.proposal_id), "binding-question"
    )
    row_reply(port, "binding-answer", (question["subjects"][0], "included"))
    port.register_opening(
        ledger.binding,
        source,
        view,
        draft,
        "binding-answer",
        "binding-opening",
        "opening_correct",
        question,
    )
    with api._read() as (service, conn):
        captured = service._source(conn, source.source_id, allow_archived=True)
        previous = _previous(conn, source.source_id)
        args = (
            service,
            conn,
            captured,
            view.bundle_ref,
            "binding-answer",
            "opening_correct",
            "binding-opening",
            previous,
        )
        current = _compose(*args)
        assert current.binding == ledger.binding
        assert _compose(*args, binding=current.binding) == current
        with pytest.raises(LifecycleError, match="stale_proposal_binding"):
            _compose(*args, binding=replace(current.binding, generation="alternate"))


def test_normal_opening_resolution_ignores_host_decimal_precision_and_traps(live):
    from decimal import Inexact, Rounded, localcontext

    ledger, port, _, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement(ledger, port, movements, account, "precision-expense")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        complete = complete_dependencies(
            ledger,
            port,
            api,
            source,
            initial,
            reader,
            account,
            "precision-opening",
            opening_command(),
            {"expense": "included"},
        )
        card, _ = finish(api, complete, "precision-opening")
        assert card.material["opening_effects"]["after_balance"] == "900.00"
        assert reader.collect().balances[0].balance_amount == "900.00"


def choice_rows():
    return [
        {
            "movement_fact_ref": {
                "record_id": "row-a",
                "version": 1,
                "fact_sha256": "a" * 64,
                "commit_seq": 1,
            },
            "impact_role": "debit",
        },
        {
            "movement_fact_ref": {
                "record_id": "row-b",
                "version": 1,
                "fact_sha256": "b" * 64,
                "commit_seq": 2,
            },
            "impact_role": "credit",
        },
    ]


@pytest.mark.parametrize(
    ("raw", "treatment"),
    [
        ("included", "included"),
        ("already included", "included"),
        ("已包含", "included"),
        ("已经包含", "included"),
        ("excluded", "excluded"),
        ("not included", "excluded"),
        ("未包含", "excluded"),
        ("尚未包含", "excluded"),
        ("unknown", "unknown"),
        ("uncertain", "unknown"),
        ("unsure", "unknown"),
        ("not sure", "unknown"),
        ("不确定", "unknown"),
        ("未知", "unknown"),
        ("不清楚", "unknown"),
        ("  NOT INCLUDED  ", "excluded"),
        ("\n 已包含 \n", "included"),
    ],
)
def test_complete_opening_choice_pure_accepts_only_full_bare_choice(raw, treatment):
    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import _parse_opening_choices

    rows = choice_rows()
    start = len(raw) - len(raw.lstrip())
    span = None if treatment == "unknown" else [start, start + len(raw.strip())]
    selected = {**rows[1], "span": span}
    result = _parse_opening_choices(raw, rows, [selected])
    assert list(result) == [("row-b", "credit")]
    assert result[("row-b", "credit")] == {**selected, "treatment": treatment}


@pytest.mark.parametrize(
    ("raw", "quote"),
    [
        ("不是已包含", "已包含"),
        ("并非已包含", "已包含"),
        ("没有已经包含", "已经包含"),
        ("未确定是否已包含", "已包含"),
        ("已包含，但我不确定", "已包含"),
        ("已包含或未包含", "已包含"),
        ("I am unsure whether it is included", "included"),
        ("included or excluded", "included"),
        ("It was not included", "included"),
        ("not included", "included"),
        ("not already included", "already included"),
        ("not excluded", "excluded"),
        ("unincluded", "included"),
        ("includedness", "included"),
        ("row-a:debit=不是已包含", "已包含"),
        ("1=included or excluded", "included"),
        ("1=included; extra words", "included"),
        ("1=included;", "included"),
        ("included; 1=excluded", "included"),
        ("1=included\n\n2=excluded", "included"),
    ],
)
def test_complete_opening_choice_pure_rejects_free_text_even_with_authentic_literal_span(
    raw, quote
):
    from types import SimpleNamespace

    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import _answer

    start = raw.index(quote)
    selected = {
        **choice_rows()[0],
        "span": [start, start + len(quote)],
        "treatment": "excluded" if quote == "excluded" else "included",
    }
    with pytest.raises(LifecycleError):
        _answer(selected, SimpleNamespace(response={"raw_input": raw}))


@pytest.mark.parametrize(
    "raw",
    [
        "row-a:debit=included; row-b:credit=not included",
        "1=已包含; 2=未包含",
        "2=not included\n1=already included",
    ],
)
def test_complete_opening_choice_pure_consumes_all_multirow_input(raw):
    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import _parse_opening_choices

    rows = choice_rows()
    positive = (
        "already included"
        if "already included" in raw
        else "已包含"
        if "已包含" in raw
        else "included"
    )
    negative = "未包含" if "未包含" in raw else "not included"
    p, n = raw.index(positive), raw.index(negative)
    selections = [
        {**rows[0], "span": [p, p + len(positive)]},
        {**rows[1], "span": [n, n + len(negative)]},
    ]
    result = _parse_opening_choices(raw, rows, selections)
    assert result[("row-a", "debit")]["treatment"] == "included"
    assert result[("row-b", "credit")]["treatment"] == "excluded"


@pytest.mark.parametrize(
    "raw",
    [
        "1=included; 1=excluded",
        "row-a:debit=included; 1=excluded",
        "row-a:debit=included; 2=excluded",
        "row-a:credit=included",
        "3=included",
        "01=included",
        "1=included; 2=maybe",
    ],
)
def test_complete_opening_choice_pure_rejects_duplicate_undelivered_or_mixed_rows(raw):
    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import _parse_opening_choices

    rows = choice_rows()
    start = raw.index("included")
    selections = [{**rows[0], "span": [start, start + 8]}]
    if raw == "row-a:debit=included; 2=excluded":
        negative = raw.index("excluded")
        selections.append({**rows[1], "span": [negative, negative + 8]})
    with pytest.raises(LifecycleError, match="Mixed" if len(selections) == 2 else None):
        _parse_opening_choices(raw, rows, selections)


@pytest.mark.parametrize(
    "variant", ["bare_many", "omitted_row", "wrong_version", "inner_span", "unknown_positive"]
)
def test_complete_opening_choice_pure_binds_actual_rows_and_complete_choice(variant):
    from types import SimpleNamespace

    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import (
        _answer,
        _parse_opening_choices,
    )

    rows = choice_rows()
    if variant == "unknown_positive":
        with pytest.raises(LifecycleError):
            _answer(
                {**rows[0], "span": None, "treatment": "included"},
                SimpleNamespace(response={"raw_input": "不确定"}),
            )
        return
    raw = "included" if variant == "bare_many" else "1=not included"
    selections = [{**rows[0], "span": [2, len(raw)]}]
    if variant in {"bare_many", "omitted_row"}:
        selections.append({**rows[1], "span": [0, 8]})
    if variant == "wrong_version":
        selections[0]["movement_fact_ref"] = {**rows[0]["movement_fact_ref"], "version": 2}
    if variant == "inner_span":
        selections[0]["span"] = [6, len(raw)]
    with pytest.raises(LifecycleError):
        _parse_opening_choices(raw, rows, selections)


def test_complete_opening_choice_pure_keeps_explicit_unknown_unresolved_in_multirow_reply():
    from finance_core.lifecycle_v2.daily_v1.opening_contracts_v1 import _parse_opening_choices

    rows = choice_rows()
    raw = "2=不确定; 1=already included"
    start = raw.index("already included")
    selected = [{**rows[0], "span": [start, len(raw)]}, {**rows[1], "span": None}]
    parsed = _parse_opening_choices(raw, rows, selected)
    assert parsed[("row-a", "debit")]["treatment"] == "included"
    assert parsed[("row-b", "credit")]["treatment"] == "unknown"
    assert parsed[("row-b", "credit")]["span"] is None


def test_complete_opening_choice_normal_literal_span_cannot_promote_negative_reply(live):
    from dataclasses import asdict

    from finance_core.lifecycle_v2.daily_v1 import VerifiedOpeningInstructionLinkV1
    from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref

    ledger, port, _, movements, reader, api, source, initial, account, _ = prepare_opening(live)
    movement(ledger, port, movements, account, "literal-expense")
    view = reader.get_account(account)
    draft = opening_proposal(
        ledger, port, api, source, initial, view, "literal-opening", opening_command()
    )
    original_callback = port.opening_instruction_link_v1
    saved = {}

    def literal_relation(conn, identity, expected_target):
        if identity not in saved:
            return original_callback(conn, identity, expected_target)
        event, result = saved[identity]
        from finance_core.lifecycle_v2.daily_v1.contracts import strict_json

        material = strict_json(result["link_json"])
        assert material["association_ref"]["sha256"] == digest(event)
        assert material["target"] == event["target"]
        assert material["selections"] == [event["selection"]]
        assert port.sources[identity].original_sha256 == event["reply_sha256"]
        question = event["question"]
        assert port.opening_questions[question["question_id"] + ":1"] == question
        assert (
            digest(port.opening_deliveries[question["delivery_ref"]["evidence_id"]])
            == question["delivery_ref"]["sha256"]
        )
        return VerifiedOpeningInstructionLinkV1(**result)

    port.opening_instruction_link_v1 = literal_relation

    def receive_literal(previous, identity, raw, span, treatment):
        # Independently saved actual receive metadata, before any expected or
        # provider callback. It authenticates literal text, never a choice code.
        question = port.deliver_opening_question(
            api.opening_questions_v1(previous.proposal_id), "literal-q-" + identity
        )
        row = question["subjects"][0]
        actual = port.add_source(identity, raw)
        selection = {
            "movement_fact_ref": row["movement_fact_ref"],
            "impact_role": row["impact_role"],
            "span": span,
        }
        target = {
            "binding": asdict(ledger.binding),
            "source_id": source.source_id,
            "source_sha256": source.source_sha256,
            "source_instruction_ref": instruction_ref(asdict(port.sources["source1"])),
            "instruction_ref": instruction_ref(asdict(port.sources["literal-opening"])),
            "response_ref": instruction_ref(asdict(actual)),
            "record_id": account,
            "command_kind": "opening_correct",
            "current_proposal": {
                k: asdict(previous)[k] for k in ("proposal_id", "version", "proposal_sha256")
            },
            "target_fact_ref": asdict(view.bundle_ref),
        }
        event = {
            "target": target,
            "reply_sha256": actual.original_sha256,
            "question": question,
            "selection": selection,
        }
        result = {
            "evidence_id": "literal-link-" + identity,
            "link_json": canonical(
                {
                    "format": "finance-daily-opening-instruction-link-v1",
                    "mode": "dependency_answers",
                    "target": target,
                    "association_ref": {
                        "evidence_id": "actual-receive-" + identity,
                        "sha256": digest(event),
                    },
                    "question": question,
                    "selections": [selection],
                }
            ),
        }
        saved[identity] = event, result
        port.commands[identity] = opening_command(answers=[{**selection, "treatment": treatment}])
        return api.propose_opening_change_v2(
            account,
            identity,
            view.bundle_ref,
            "propose-" + identity,
            reply_evidence_id=identity,
            command_kind="opening_correct",
            opening_instruction_evidence_id="literal-opening",
        )

    before = financial_rows(ledger)
    with pytest.raises(LifecycleError, match="complete opening answer"):
        receive_literal(draft, "literal-negative", "不是已包含", [2, 5], "included")
    assert financial_rows(ledger) == before
    assert api.pending()[0]["proposal"]["proposal_id"] == draft.proposal_id
    unknown = receive_literal(draft, "literal-unknown", "不确定", None, "unknown")
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(unknown.proposal_id, unknown.version)
    assert financial_rows(ledger) == before
    positive = receive_literal(unknown, "literal-positive", "已包含", [0, 3], "included")
    card, _ = finish(api, positive, "literal-positive")
    entry = card.material["after_values"]["opening_dependency_map"]["entries"][0]
    assert entry["answer_ref"]["quote"] == "已包含"
    assert entry["treatment"] == "included"
    assert reader.collect().balances[0].balance_amount == "900.00"


def scalar_opening_chain(count):
    """Traversal metadata only: never valid/authenticated or persisted ledger rows."""
    return {
        version: {
            "proposal_id": "scalar-" + str(version),
            "version": version,
            "source_id": "scalar-source",
            "record_id": "scalar-record",
            "observation_json": canonical(
                {
                    "format": "finance-daily-opening-observation-v2",
                    "expected": {
                        "source_id": "scalar-source",
                        "record_id": "scalar-record",
                        "instruction": {"evidence_id": "scalar-original"},
                        "reply_evidence_id": "scalar-original"
                        if version == 1
                        else "scalar-reply-" + str(version),
                        "expected_proposal_version": version - 1,
                        "current_proposal_id": "scalar-" + str(version - 1)
                        if version > 1
                        else None,
                    },
                    "observation": {},
                }
            ),
        }
        for version in range(1, count + 1)
    }


def scalar_chain_verifier(monkeypatch, rows, *, fail_at=None, fingerprint=None):
    """Use real traversal/query helper; spy on the independent per-item seam.

    This proves traversal and call ordering, not financial authentication.
    Normal API tests below execute the real authentication kernel and ports.
    """
    from types import SimpleNamespace

    from finance_core.lifecycle_v2.daily_v1 import opening_changes_v1 as module

    queries, visits = [], []

    class ScalarConnection:
        def execute(self, query, parameters):
            assert "l2_proposals WHERE source_id=? AND version=?" in query
            source, version = parameters
            assert source == "scalar-source"
            queries.append(version)
            return SimpleNamespace(fetchone=lambda: rows.get(version))

    monkeypatch.setattr(module, "decode_expected", lambda value: SimpleNamespace(**value))
    monkeypatch.setattr(module, "_fingerprint", fingerprint or (lambda _: "scalar-stable"))

    def verify_item(_service, _conn, item):
        version = item["version"]
        visits.append(version)
        if version == fail_at:
            raise LifecycleError("scalar independent item authentication failed")
        assert visits == list(range(1, version + 1))
        return SimpleNamespace(verified_version=version)

    monkeypatch.setattr(module, "_verify_proposal_item", verify_item)
    return module, ScalarConnection(), queries, visits


@pytest.mark.parametrize("count", [1500, 4096])
def test_opening_chain_pure_iterates_all_items_oldest_first_within_existing_limit(
    monkeypatch, count
):
    import sys

    rows = scalar_opening_chain(count)
    module, conn, queries, visits = scalar_chain_verifier(monkeypatch, rows)
    original_limit = sys.getrecursionlimit()
    assert count <= module.MAX_PROPOSALS
    result = module.verify_proposal(object(), conn, rows[count])
    assert result.verified_version == count
    assert visits == list(range(1, count + 1))
    assert queries == list(range(count - 1, 0, -1))
    assert sys.getrecursionlimit() == original_limit


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "wrong_version",
        "wrong_source",
        "wrong_record",
        "wrong_expected_source",
        "wrong_identity",
        "wrong_instruction",
        "wrong_parent",
        "repeated",
        "over_limit",
    ],
)
def test_opening_chain_pure_refuses_missing_wrong_or_repeated_predecessors(monkeypatch, invalid):
    import json

    count = 1500 if invalid != "over_limit" else 4097
    rows = scalar_opening_chain(count)
    current = rows[count]
    if invalid == "missing":
        del rows[700]
    elif invalid == "wrong_version":
        rows[700]["version"] = 699
    elif invalid in {"wrong_source", "wrong_record", "wrong_identity"}:
        key = {
            "wrong_source": "source_id",
            "wrong_record": "record_id",
            "wrong_identity": "proposal_id",
        }[invalid]
        rows[700][key] = "other-identity"
    elif invalid in {"wrong_expected_source", "wrong_instruction", "wrong_parent"}:
        envelope = json.loads(rows[700]["observation_json"])
        if invalid == "wrong_expected_source":
            envelope["expected"]["source_id"] = "other-source"
        elif invalid == "wrong_instruction":
            envelope["expected"]["instruction"] = {"evidence_id": "other-instruction"}
        else:
            envelope["expected"]["current_proposal_id"] = "other-parent"
        rows[700]["observation_json"] = canonical(envelope)
    elif invalid == "repeated":
        rows[700]["proposal_id"] = current["proposal_id"]
        envelope = json.loads(rows[701]["observation_json"])
        envelope["expected"]["current_proposal_id"] = current["proposal_id"]
        rows[701]["observation_json"] = canonical(envelope)
    module, conn, _, visits = scalar_chain_verifier(monkeypatch, rows)
    with pytest.raises(LifecycleError):
        module.verify_proposal(object(), conn, current)
    assert visits == []


def test_opening_chain_pure_stops_on_independent_item_failure(monkeypatch):
    rows = scalar_opening_chain(1500)
    module, conn, _, visits = scalar_chain_verifier(monkeypatch, rows, fail_at=751)
    with pytest.raises(LifecycleError, match="independent item"):
        module.verify_proposal(object(), conn, rows[1500])
    assert visits == list(range(1, 752))


def test_opening_chain_pure_retains_whole_operation_fingerprint(monkeypatch):
    rows = scalar_opening_chain(3)
    fingerprints = iter(("before", "changed"))
    module, conn, _, visits = scalar_chain_verifier(
        monkeypatch, rows, fingerprint=lambda _: next(fingerprints)
    )
    with pytest.raises(LifecycleError, match="protected opening chain state"):
        module.verify_proposal(object(), conn, rows[3])
    assert visits == [1, 2, 3]


def test_opening_chain_normal_unknown_restart_confirmation_fresh_instruction_and_snapshot(
    live, request, tmp_path, monkeypatch
):
    import sqlite3

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.instance_admission.authority import _logical_database_digest
    from finance_core.lifecycle_v2 import LedgerService
    from finance_core.lifecycle_v2.reader import audit_evidence_connection

    ledger, port, daily, movements, reader, api, source, initial, account, cfg = prepare_opening(
        live
    )
    _, expense_card, expense = movement(ledger, port, movements, account, "history-expense")
    originals = financial_rows(ledger)
    draft = opening_proposal(
        ledger,
        port,
        api,
        source,
        initial,
        reader.get_account(account),
        "history-opening",
        opening_command(),
    )
    links, observations = [], []
    real_link, real_observation = port.opening_instruction_link_v1, port.opening_proposal_v2

    def require_read_context(conn):
        if type(conn) is sqlite3.Connection:
            # Independent immutable snapshot audit has its original raw
            # read-only connection, without a required explicit transaction.
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        else:
            assert conn.in_transaction

    def read_link(conn, identity, expected):
        require_read_context(conn)
        links.append(identity)
        return real_link(conn, identity, expected)

    def read_observation(conn, identity, expected):
        require_read_context(conn)
        observations.append(identity)
        return real_observation(conn, identity, expected)

    monkeypatch.setattr(port, "opening_instruction_link_v1", read_link)
    monkeypatch.setattr(port, "opening_proposal_v2", read_observation)
    previous = draft
    for number in (1, 2):
        question = port.deliver_opening_question(
            api.opening_questions_v1(previous.proposal_id), "history-q-" + str(number)
        )
        identity = "history-unknown-" + str(number)
        answers = row_reply(port, identity, (question["subjects"][0], "unknown"))
        previous = opening_proposal(
            ledger,
            port,
            api,
            source,
            previous,
            reader.get_account(account),
            identity,
            opening_command(answers=answers),
            "history-opening",
            question,
        )
    assert financial_rows(ledger) == originals
    binding = ledger.binding
    ledger.owner.close()
    owner = acquire_production_owner(cfg)
    request.addfinalizer(owner.close)
    ledger = LedgerService(owner, port, binding, clock=lambda: NOW)
    api, reader = DailyOpeningChangeServiceV1(ledger), DailyLedgerReaderV1(ledger)
    movements = DailyMovementServiceV1(ledger)
    assert api.pending()[0]["values"]["missing_fields"]
    question = port.deliver_opening_question(
        api.opening_questions_v1(previous.proposal_id), "history-q-3"
    )
    answers = row_reply(port, "history-unknown-3", (question["subjects"][0], "unknown"))
    previous = opening_proposal(
        ledger,
        port,
        api,
        source,
        previous,
        reader.get_account(account),
        "history-unknown-3",
        opening_command(answers=answers),
        "history-opening",
        question,
    )
    links.clear()
    observations.clear()
    pending = api.pending()
    required = {"history-opening", "history-unknown-1", "history-unknown-2", "history-unknown-3"}
    assert set(links) == set(observations) == required
    assert pending[0]["values"]["missing_fields"]
    assert financial_rows(ledger) == originals
    question = port.deliver_opening_question(
        api.opening_questions_v1(previous.proposal_id), "history-final-q"
    )
    answers = row_reply(port, "history-positive", (question["subjects"][0], "included"))
    complete = opening_proposal(
        ledger,
        port,
        api,
        source,
        previous,
        reader.get_account(account),
        "history-positive",
        opening_command(answers=answers),
        "history-opening",
        question,
    )
    card, receipt = finish(api, complete, "history-first")
    assert reader.collect().balances[0].balance_amount == "900.00"
    # A new genuine original instruction breaks no retained older audit chain.
    fresh = complete_dependencies(
        ledger,
        port,
        api,
        source,
        complete,
        reader,
        account,
        "history-fresh",
        opening_command(amount="850"),
        {"expense": "excluded"},
    )
    fresh_card, _ = finish(api, fresh, "history-fresh")
    assert fresh_card.material["instruction_ref"]["evidence_id"] == "history-fresh"
    result = reader.collect_with_effects_v1()
    assert result.collection.balances[0].balance_amount == "841.00"
    assert result.collection.expense_by_currency == (("MYR", "9.00"),)
    assert (
        ledger.reader().full_audit()["fact_version_count"]
        == result.collection.fact_version_count
        == 4
    )
    assert (
        api.lookup_commit_receipt(
            "confirm-history-first",
            review_id=card.review_id,
            action_evidence_id="action-history-first",
        )
        == receipt
    )
    assert (
        movements.lookup_commit_receipt(
            "confirm-history-expense",
            review_id=expense_card.review_id,
            action_evidence_id="action-history-expense",
        )
        == expense
    )
    cut = ledger.seal_snapshot(tmp_path / "history-chain.sqlite")
    with sqlite3.connect(
        (tmp_path / "history-chain.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
    ) as copy:
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA query_only=ON")
        audit = audit_evidence_connection(copy, port, binding)
        assert audit["fact_version_count"] == result.collection.fact_version_count
        assert audit["event_count"] == cut.commit_seq == result.read_identity.event_seq
        assert (
            _logical_database_digest(copy)
            == cut.logical_database_sha256
            == result.read_identity.logical_database_sha256
        )
    current = {r[:2]: r[2:] for r in financial_rows(ledger)}
    assert all(current[r[:2]] == r[2:] for r in originals)


def test_owned_pending_opening_pages_survive_original_owner_and_immutable_cut(
    live, tmp_path, request, monkeypatch
):
    import sqlite3
    from copy import deepcopy
    from dataclasses import asdict, replace

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.lifecycle_v2 import LedgerService
    from finance_core.lifecycle_v2.daily_v1 import _opening_proposal_pages_v1 as pages
    from finance_core.lifecycle_v2.daily_v1.contracts import strict_json
    from finance_core.lifecycle_v2.reader import audit_evidence_connection

    ledger, port, _, movements, reader, api, source, previous, account, cfg = prepare_opening(live)
    movement(ledger, port, movements, account, "expense")
    movement(ledger, port, movements, account, "income", fields(account, "2", "income"))
    view = reader.get_account(account)
    original_facts = financial_rows(ledger)
    command = opening_command()

    def register(identity, padding):
        port.add_source(identity, "wrong opening; set 900 MYR " + "a" * padding)
        port.register_opening(ledger.binding, source, view, previous, identity)
        port.commands[identity] = command
        return api.expected_opening_change_v2(account, view.bundle_ref, identity, "opening_correct")

    probe = register("page-shape", 0)
    probe_seal = {
        "format": "finance-daily-opening-observation-v2",
        "expected": asdict(probe),
        "observation": asdict(port.opening_proposal_v2(None, "page-shape", probe)),
    }
    expected = register("page-start", (131072 + 1000 - len(canonical(probe_seal))) // 2)
    args = account, "page-start", view.bundle_ref, "propose-page-start"
    kwargs = {"reply_evidence_id": "page-start", "command_kind": "opening_correct"}
    with pytest.raises(LifecycleError, match="Bounded exact JSON"):
        api.propose_opening_change_v2(*args, **kwargs)
    prior_event_count = ledger.reader().full_audit()["event_count"]
    with monkeypatch.context() as limits:
        limits.setattr(pages, "_CUT_BYTES", 131072)
        with pytest.raises(LifecycleError, match="storage budget"):
            api.propose_opening_change_v2(*args, **kwargs, page_initial_dependencies=True)
    assert ledger.reader().full_audit()["event_count"] == prior_event_count
    draft = api.propose_opening_change_v2(*args, **kwargs, page_initial_dependencies=True)
    assert draft == api.propose_opening_change_v2(*args, **kwargs, page_initial_dependencies=True)
    with lifecycle_read_operation(ledger.owner) as conn:
        row = conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (draft.proposal_id,)
        ).fetchone()
        header = strict_json(row["observation_json"])
        assert len(canonical(pages.read_sealed(row)).encode()) > 131072
        assert len(row["observation_json"].encode()) <= 131072
        manifest = header["expected"]["dependencies"]
        assert manifest["owner"]["proposal_id"] == draft.proposal_id
        assert manifest["closure_sha256"] == digest(expected.dependencies)
        assert manifest["count"] == 2
        assert pages.read_sealed(row)["expected"] == asdict(expected)
        # Encoding scale only; the 100 distinct identifiers are synthetic codec inputs.
        template = expected.dependencies[0]
        shaped = [
            {**template, "fact_ref": {**template["fact_ref"], "record_id": f"scale-{i:03}"}}
            for i in range(100)
        ]
        scaled_header, scaled_values = pages.encode(
            replace(expected, dependencies=shaped),
            port.opening_observations["page-start"],
            pages.read_values(row),
            "scale-proposal",
            16,
            manifest["owner"]["previous_event_sha256"],
        )
        assert len(scaled_values["pages"]) >= 7
        assert len(canonical(scaled_values).encode()) > 131072
        assert (
            pages.read_sealed(
                {
                    "observation_json": canonical(scaled_header),
                    "values_json": canonical(scaled_values),
                }
            )["expected"]["dependencies"]
            == shaped
        )
        for incomplete, message in (
            (scaled_values["pages"][:-1], "Opening page group differs"),
            (list(reversed(scaled_values["pages"])), "Opening dependency page differs"),
        ):
            with pytest.raises(LifecycleError, match=message):
                pages.read_sealed(
                    {
                        "observation_json": canonical(scaled_header),
                        "values_json": canonical({**scaled_values, "pages": incomplete}),
                    }
                )
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(draft.proposal_id, draft.version)
    before = api.opening_questions_v2(draft.proposal_id)
    binding = ledger.binding
    ledger.owner.close()
    owner = acquire_production_owner(cfg)
    request.addfinalizer(owner.close)
    ledger = LedgerService(owner, port, binding, clock=lambda: NOW)
    api = DailyOpeningChangeServiceV1(ledger)
    assert api.opening_questions_v2(draft.proposal_id) == before
    assert len(api.pending()[0]["values"]["missing_fields"]) == 2
    assert financial_rows(ledger) == original_facts
    source_audit = ledger.reader().full_audit()
    path = tmp_path / "paged-opening.sqlite"
    cut = ledger.seal_snapshot(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as copy:
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA query_only=ON")
        restored_port = deepcopy(port)
        restored_port.commands.clear()
        assert audit_evidence_connection(copy, restored_port, binding) == source_audit
        with monkeypatch.context() as limits:
            limits.setattr(pages, "_CUT_BYTES", 131072)
            with pytest.raises(LifecycleError, match="storage budget"):
                audit_evidence_connection(copy, restored_port, binding)
    assert cut.commit_seq == source_audit["event_count"]
