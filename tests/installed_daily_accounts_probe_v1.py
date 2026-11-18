"""Actual opt-in daily flow and fresh-process snapshot proof from installed bytes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


def main():
    installed, helpers, outside, checkout = map(Path, sys.argv[1:5])
    mode = sys.argv[5] if len(sys.argv) > 5 else "flow"
    sys.path[:0] = [str(installed), str(helpers), str(helpers / "runtime-dependencies")]
    from daily_account_support_v1 import NOW, OpeningPort, command, unknown_time

    import finance_core
    from finance_core.instance_admission import (
        InstanceAdmissionError,
        TrustedInstanceConfig,
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
    )
    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from finance_core.lifecycle_v2 import (
        LedgerService,
        RuntimeBinding,
        VerifiedAction,
        VerifiedDelivery,
        VerifiedSource,
    )
    from finance_core.lifecycle_v2.contracts import LifecycleError
    from finance_core.lifecycle_v2.daily_v1 import (
        DAILY_CONTRACT_VERSION,
        DailyAccountReaderV1,
        DailyAccountServiceV1,
        DailyLedgerReaderV1,
        DailyMovementServiceV1,
        DailyOpeningChangeServiceV1,
        VerifiedDailyProposalV1,
        VerifiedMovementProposalV1,
        VerifiedOpeningProposalV2,
    )
    from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1, EffectiveReadError
    from finance_core.profile_layout import product_root, profile_root

    scratch = outside / ("daily-accounts-" + installed.name)
    layout = "mac" if sys.platform == "darwin" else "linux"
    support = scratch / ("Application Support" if layout == "mac" else "finance-codex")
    product = product_root(support, layout)
    profile = profile_root(support, "synthetic-daily", layout)
    if mode == "flow":
        scratch.mkdir(mode=0o700)
        support.mkdir(mode=0o700)
        if product != support:
            product.mkdir(mode=0o700)
        for p in (
            profile.parent,
            profile,
            profile / "runtime",
            profile / "runtime/database",
            profile / "workspace",
            profile / "workspace/database",
            profile / "backups",
            profile / "work",
            profile / "restore",
            scratch / "control",
        ):
            p.mkdir(mode=0o700)
        manifest = profile / "profile.json"
        manifest.write_text(
            json.dumps(
                {
                    "profile_id": "synthetic-daily",
                    "runtime_root": str(profile / "runtime"),
                    "workspace_root": str(profile / "workspace"),
                }
            )
        )
        manifest.chmod(0o600)
    os.environ["FINANCE_RUNTIME_ROOT"] = str(profile / "runtime")
    cfg = TrustedInstanceConfig(
        scratch / "control",
        support,
        "synthetic-daily",
        layout=layout,
        instance_id="synthetic-daily-instance",
        config_identity="synthetic-daily-config-v1",
        core_identity="synthetic-daily-core-v1",
        approval_verifier=lambda ref, action, binding: ref == "approved",
    )
    owner = None
    try:
        if mode == "flow":
            prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
            admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
        owner = acquire_production_owner(cfg)
        port = OpeningPort()
        state_path = scratch / "trusted-synthetic-port.json"
        if mode == "readback":
            state = json.loads(state_path.read_text())
            for key, cls in (
                ("sources", VerifiedSource),
                ("observations", VerifiedDailyProposalV1),
                ("movement_observations", VerifiedMovementProposalV1),
                ("opening_observations", VerifiedOpeningProposalV2),
                ("deliveries", VerifiedDelivery),
                ("actions", VerifiedAction),
            ):
                setattr(port, key, {k: cls(**v) for k, v in state[key].items()})
            port.commands = state["commands"]
            for key in (
                "movement_sources",
                "movement_latest",
                "movement_links",
                "movement_link_origins",
                "movement_questions",
                "movement_delivered_questions",
                "movement_latest_question",
                "opening_links",
                "opening_origins",
                "opening_questions",
                "opening_deliveries",
                "latest_opening_questions",
                "opening_reply_associations",
            ):
                setattr(port, key, state[key])
        binding = RuntimeBinding(cfg.instance_id, "daily-generation-1", "user", "chat")
        ledger = LedgerService(owner, port, binding, clock=lambda: NOW)
        daily = DailyAccountServiceV1(ledger)
        reader = DailyAccountReaderV1(ledger)
        if mode == "flow":
            port.add_source(
                "source1", "Create my MYR credit card account; opening not yet specified"
            )
            source = ledger.capture("source1", "capture1")
            source_reader = DailyLedgerReaderV1(ledger)
            assert source_reader.source(source.source_id) == port.sources["source1"]
            actual_source_callback = port.source
            for control in ("commit", "rollback", "close"):
                source_calls = []

                def guarded_source(conn, identity):
                    source_calls.append((id(conn), conn.in_transaction))
                    if len(source_calls) == 2:
                        try:
                            getattr(conn, control)()
                        except (InstanceAdmissionError, sqlite3.DatabaseError):
                            pass
                    return actual_source_callback(conn, identity)

                port.source = guarded_source
                try:
                    source_reader.source(source.source_id)
                except InstanceAdmissionError as exc:
                    assert "read-only scope" in str(exc)
                else:
                    raise AssertionError(
                        "Daily source callback controlled its transaction/connection"
                    )
                finally:
                    port.source = actual_source_callback
                assert len(source_calls) == 2
                assert len({identity for identity, _ in source_calls}) == 1
                assert all(in_transaction for _, in_transaction in source_calls)
            assert source_reader.source(source.source_id) == port.sources["source1"]
            port.commands["draft"] = command(fields={"name": "Daily Card", "currency": "MYR"})
            draft = daily.propose_account_create(source.source_id, "draft", 0, "draft-propose")
            assert daily.pending()[0]["values"]["missing_fields"] == ["account_type", "opening"]
            try:
                daily.prepare_review(draft.proposal_id, draft.version)
            except LifecycleError as exc:
                assert "incomplete_proposal" in str(exc)
            else:
                raise AssertionError("Incomplete proposal acquired review")
            assert reader.collect_accounts().accounts == ()
            port.add_source("complete", "Credit card opening debt MYR 300; time unknown")
            port.commands["complete"] = command(
                fields={
                    "name": "Daily Card",
                    "account_type": "credit_card",
                    "currency": "MYR",
                    "opening": {"amount": "-300", "as_of": unknown_time()},
                }
            )
            proposal = daily.propose_account_create(
                source.source_id, "complete", 1, "complete-propose", reply_evidence_id="complete"
            )
            card = daily.prepare_review(proposal.proposal_id, proposal.version)
            daily.record_delivery(card.review_id, "delivery-create")

            def forbidden_callback(conn, *args):
                for operation in (
                    lambda: conn.execute(
                        "INSERT INTO participants(public_id,display_name) "
                        "VALUES('callback','callback')"
                    ),
                    conn.commit,
                ):
                    try:
                        operation()
                    except (sqlite3.DatabaseError, InstanceAdmissionError):
                        pass

            port.on_action = forbidden_callback
            try:
                daily.confirm(card.review_id, "action-create", "confirm-create")
            except InstanceAdmissionError as exc:
                assert "read-only scope" in str(exc)
            else:
                raise AssertionError("Evidence callback acquired financial write authority")
            port.on_action = None
            with lifecycle_read_operation(owner) as fresh:
                assert (
                    fresh.execute(
                        "SELECT count(*) FROM participants WHERE public_id='callback'"
                    ).fetchone()[0]
                    == 0
                )
                assert fresh.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 0
                assert fresh.execute("SELECT count(*) FROM l2_decisions").fetchone()[0] == 0
            first = daily.confirm(card.review_id, "action-create", "confirm-create")
            account = first.account_ref.account_id
            original_opening = first.opening_ref
            assert reader.get_account(account).opening_amount == "-300.00"

            latest_account_proposal = proposal

            def change(kind, fields, identity, reason=None):
                nonlocal latest_account_proposal
                view = reader.get_account(account)
                raw = "Explicit account instruction; reason: " + (reason or "requested update")
                port.add_source(identity, raw)
                span = [raw.index(reason), len(raw)] if reason else None
                port.commands[identity] = command(kind, fields, span)
                proposal = daily.propose_account_change(
                    account,
                    identity,
                    view.bundle_ref,
                    "propose-" + identity,
                    reply_evidence_id=identity,
                    command_kind=kind,
                )
                latest_account_proposal = proposal
                review = daily.prepare_review(proposal.proposal_id, proposal.version)
                daily.record_delivery(review.review_id, "delivery-" + identity)
                return daily.confirm(review.review_id, "action-" + identity, "confirm-" + identity)

            change("account_rename", {"name": "Renamed Card"}, "rename")
            assert reader.get_account(account).opening_ref == original_opening
            change("opening_void", {}, "void", "incorrect opening")
            assert reader.get_account(account).opening_amount is None
            change("opening_set", {"amount": "0", "as_of": unknown_time()}, "new-opening")
            current = reader.get_account(account)
            assert (
                current.opening_amount == "0.00"
                and current.opening_ref.opening_event_id != original_opening.opening_event_id
            )
            assert len(reader.opening_history(account, original_opening.opening_event_id)) == 2
            assert (
                daily.lookup_commit_receipt(
                    "confirm-create", review_id=card.review_id, action_evidence_id="action-create"
                )
                == first
            )
            try:
                EffectiveLedgerReaderV1(ledger).collect()
            except EffectiveReadError as exc:
                assert exc.code == "unsupported_fact_shape"
            else:
                raise AssertionError("Old complete reader admitted daily")
            audit = ledger.reader().full_audit()
            assert audit["fact_version_count"] == 4
            movements = DailyMovementServiceV1(ledger)
            unified = DailyLedgerReaderV1(ledger)

            movement_reviews = {}

            def movement(identity, kind="movement_create", target=None, amount="9", income=False):
                raw = "excluded; incorrect amount"
                port.add_source(identity, raw)
                fields = (
                    {}
                    if kind == "movement_void"
                    else {
                        "kind": "income" if income else "expense",
                        "business_date": "2026-11-17",
                        "description": "Synthetic salary" if income else "Synthetic coffee",
                        "category": "salary" if income else "food",
                        "account_id": account,
                        "account_amount": amount,
                        "original_amount": amount if income else "1000",
                        "original_currency": "MYR" if income else "JPY",
                        "funds_time": unknown_time(),
                        "coverage": {"answer": "excluded", "span": [0, 8]},
                    }
                )
                port.commands[identity] = {
                    "format": "finance-daily-movement-command-v1",
                    "command_kind": kind,
                    "fields": fields,
                    "reason_span": [10, len(raw)] if target else None,
                }
                if target is None:
                    captured = ledger.capture(identity, "capture-" + identity)
                    port.remember_movement_source(captured, identity)
                    port.register_movement_instruction(binding, captured.source_id, identity)
                    proposal = movements.propose_movement_create(
                        captured.source_id, identity, 0, "propose-" + identity
                    )
                else:
                    port.register_movement_instruction(
                        binding, target.source_id, identity, kind, target.fact_ref
                    )
                    proposal = movements.propose_movement_change(
                        target.record_id,
                        identity,
                        target.fact_ref,
                        "propose-" + identity,
                        reply_evidence_id=identity,
                        command_kind=kind,
                    )
                port.remember_movement_proposal(proposal)
                review = movements.prepare_review(proposal.proposal_id, proposal.version)
                movement_reviews[identity] = review
                movements.record_delivery(review.review_id, "delivery-" + identity)
                return movements.confirm(
                    review.review_id, "action-" + identity, "confirm-" + identity
                )

            original_expense = movement("expense")
            assert unified.collect().balances[0].credit_card_debt == "9.00"
            assert unified.collect().expense_by_currency == (("JPY", "1000"),)
            movement("income", amount="200", income=True)
            assert unified.collect().balances[0].balance_amount == "191.00"
            target = unified.detail(original_expense.receipt.record_id)
            movement("correct-expense", "movement_correct", target, "10")
            assert unified.collect().balances[0].balance_amount == "190.00"
            movement("void-expense", "movement_void", unified.detail(target.record_id))
            assert unified.collect().balances[0].credit_card_overpayment == "200.00"
            assert (
                movements.lookup_commit_receipt(
                    "confirm-expense",
                    review_id=movement_reviews["expense"].review_id,
                    action_evidence_id="action-expense",
                )
                == original_expense
            )
            # Original movement receipt remains a separate immutable version.
            assert len(unified.history(target.record_id)) == 3
            # A genuine delivered opening-coverage question is registered
            # before the actual bare reply; callbacks only read these records.

            port.add_source("question-expense", "Record MYR 9; opening coverage unknown")
            captured = ledger.capture("question-expense", "capture-question-expense")
            port.remember_movement_source(captured, "question-expense")
            port.register_movement_instruction(binding, captured.source_id, "question-expense")
            question_fields = {
                "kind": "expense",
                "business_date": "2026-11-17",
                "description": "Synthetic coffee",
                "category": "food",
                "account_id": account,
                "account_amount": "9.00",
                "original_amount": "9.00",
                "original_currency": "MYR",
                "funds_time": unknown_time(),
                "coverage": {"answer": "unknown", "span": None},
            }
            port.commands["question-draft"] = {
                "format": "finance-daily-movement-command-v1",
                "command_kind": "movement_create",
                "fields": question_fields,
                "reason_span": None,
            }
            draft = movements.propose_movement_create(
                captured.source_id, "question-draft", 0, "propose-question-draft"
            )
            port.remember_movement_proposal(draft)
            question_account = reader.get_account(account)
            subject = {
                "account_ref": asdict(question_account.account_ref),
                "opening_ref": asdict(question_account.opening_ref),
                "kind": "expense",
                "original_amount": "9.00",
                "original_currency": "MYR",
                "account_amount": "9.00",
                "account_currency": "MYR",
                "business_date": "2026-11-17",
                "funds_time": unknown_time(),
            }
            question = port.register_opening_question(
                binding, captured.source_id, "installed-opening-question", subject
            )
            port.add_source("question-answer", "not included")
            port.register_opening_reply(question, "question-answer")
            port.commands["question-wrong-positive"] = {
                "format": "finance-daily-movement-command-v1",
                "command_kind": "movement_create",
                "fields": {**question_fields, "coverage": {"answer": "included", "span": [4, 12]}},
                "reason_span": None,
            }
            try:
                movements.propose_movement_create(
                    captured.source_id,
                    "question-wrong-positive",
                    1,
                    "propose-question-wrong-positive",
                    reply_evidence_id="question-answer",
                )
            except LifecycleError as exc:
                assert "complete genuine choice" in str(exc)
            else:
                raise AssertionError("Negative actual answer was compiled as included")
            port.commands["question-complete"] = {
                "format": "finance-daily-movement-command-v1",
                "command_kind": "movement_create",
                "fields": {**question_fields, "coverage": {"answer": "excluded", "span": [0, 12]}},
                "reason_span": None,
            }
            proposal = movements.propose_movement_create(
                captured.source_id,
                "question-complete",
                1,
                "propose-question-complete",
                reply_evidence_id="question-answer",
            )
            port.remember_movement_proposal(proposal)
            question_card = movements.prepare_review(proposal.proposal_id, proposal.version)
            movements.record_delivery(question_card.review_id, "delivery-question-card")
            question_receipt = movements.confirm(
                question_card.review_id, "independent-question-confirm", "confirm-question-expense"
            )
            assert unified.collect().balances[0].balance_amount == "191.00"

            def daily_counts():
                with daily._read() as (_, conn):
                    return tuple(
                        conn.execute(
                            "SELECT (SELECT count(*) FROM l2_facts),"
                            "(SELECT count(*) FROM l2_proposals),"
                            "(SELECT count(*) FROM l2_decisions),"
                            "(SELECT count(*) FROM l2_events WHERE kind='confirm')"
                        ).fetchone()
                    )

            assert daily_counts() == (9, 11, 9, 9)
            # One complete opening decision with actual delivered row choices.
            opening_api = DailyOpeningChangeServiceV1(ledger)
            port.add_source("mapped-opening", "wrong opening; correct to MYR 100; as-of unknown")
            view = reader.get_account(account)
            port.register_opening(binding, source, view, latest_account_proposal, "mapped-opening")
            opening_command = {
                "format": "finance-daily-opening-command-v2",
                "command_kind": "opening_correct",
                "fields": {"amount": "100", "as_of": unknown_time()},
                "reason_span": [0, 13],
                "answers": [],
            }
            port.commands["mapped-opening"] = opening_command
            opening_draft = opening_api.propose_opening_change_v2(
                account,
                "mapped-opening",
                view.bundle_ref,
                "propose-mapped-opening",
                reply_evidence_id="mapped-opening",
                command_kind="opening_correct",
            )
            assert len(opening_api.pending()[0]["values"]["missing_fields"]) == 2
            question = port.deliver_opening_question(
                opening_api.opening_questions_v1(opening_draft.proposal_id), "map-question"
            )
            raw = "; ".join(
                row["movement_fact_ref"]["record_id"] + ":" + row["impact_role"] + "=included"
                for row in question["subjects"]
            )
            port.add_source("map-answer", raw)
            port.register_opening(
                binding,
                source,
                view,
                opening_draft,
                "map-answer",
                "mapped-opening",
                question=question,
            )
            answers = []
            for row in question["subjects"]:
                marker = row["movement_fact_ref"]["record_id"] + ":" + row["impact_role"] + "="
                start = raw.index(marker) + len(marker)
                answers.append(
                    {
                        "movement_fact_ref": row["movement_fact_ref"],
                        "impact_role": row["impact_role"],
                        "treatment": "included",
                        "span": [start, start + 8],
                    }
                )
            port.commands["map-answer"] = {**opening_command, "answers": answers}
            opening_complete = opening_api.propose_opening_change_v2(
                account,
                "map-answer",
                view.bundle_ref,
                "propose-map-answer",
                reply_evidence_id="map-answer",
                command_kind="opening_correct",
                opening_instruction_evidence_id="mapped-opening",
            )
            map_card = opening_api.prepare_review(
                opening_complete.proposal_id, opening_complete.version
            )
            assert map_card.material["opening_effects"]["before_balance"] == "191.00"
            assert map_card.material["opening_effects"]["after_balance"] == "100.00"
            assert len(map_card.material["opening_effects"]["rows"]) == 2
            opening_api.record_delivery(map_card.review_id, "delivery-map-card")
            mapped = opening_api.confirm(
                map_card.review_id, "independent-map-confirm", "confirm-map"
            )
            assert (
                opening_api.confirm(map_card.review_id, "independent-map-confirm", "confirm-map")
                == mapped
            )
            assert daily_counts() == (10, 13, 10, 10)
            latest_account_proposal = opening_complete
            change("account_rename", {"name": "Mapped Card"}, "mapped-rename")
            assert daily_counts() == (11, 14, 11, 11)
            resolved = unified.collect_with_effects_v1()
            assert all(
                e.values["map_owner_fact_ref"]["version"] == mapped.receipt.version
                for e in resolved.effects
            )
            movement(
                "mapped-void", "movement_void", unified.detail(question_receipt.receipt.record_id)
            )
            assert (
                movement_reviews["mapped-void"].material["account_impact_changes"][0][
                    "balance_delta"
                ]
                == "0.00"
            )
            assert daily_counts() == (12, 15, 12, 12)
            final_fact_count, final_proposal_count, _, _ = daily_counts()
            final = unified.collect_with_effects_v1()
            assert final.collection.balances[0].balance_amount == "100.00"
            assert final.collection.balances[0].recorded_net_change == "200.00"
            assert final.collection.expense_by_currency == ()
            assert final.collection.income_by_currency == (("MYR", "200.00"),)
            cut = ledger.seal_snapshot(scratch / "snapshot.sqlite")
            state = {
                key: {k: asdict(v) for k, v in getattr(port, key).items()}
                for key in (
                    "sources",
                    "observations",
                    "movement_observations",
                    "opening_observations",
                    "deliveries",
                    "actions",
                )
            }
            state["commands"] = port.commands
            for key in (
                "movement_sources",
                "movement_latest",
                "movement_links",
                "movement_link_origins",
                "movement_questions",
                "movement_delivered_questions",
                "movement_latest_question",
                "opening_links",
                "opening_origins",
                "opening_questions",
                "opening_deliveries",
                "latest_opening_questions",
                "opening_reply_associations",
            ):
                state[key] = getattr(port, key)
            state_path.write_text(json.dumps(state))
            state_path.chmod(0o600)
            before = asdict(reader.collect_accounts().read_identity)
            owner.close()
            owner = None
            cfg.close()
            child = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).resolve()),
                    str(installed),
                    str(helpers),
                    str(outside),
                    str(checkout),
                    "readback",
                ],
                cwd=outside,
                env={"PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            reopened = json.loads(child.stdout)
            assert reopened["fact_versions"] == 12 and reopened["snapshot_fact_versions"] == 12
            assert (
                reopened["logical_database_sha256"]
                == before["logical_database_sha256"]
                == cut.logical_database_sha256
            )
            assert reopened["installed_only"] and reopened["opening_amount"] == "100.00"
            print(
                json.dumps(
                    {
                        "contract": DAILY_CONTRACT_VERSION,
                        "fact_versions": final_fact_count,
                        "proposal_count": final_proposal_count,
                        "opening_dependency_map": True,
                        "resolved_mapped_void": True,
                        "independent_instruction_question_link": True,
                        "negative_coverage_preserved": True,
                        "daily_source_guarded": True,
                        "movement_balance_correction_void": True,
                        "incomplete_preserved": True,
                        "single_confirm_bundle": True,
                        "callback_persistent_write_refused": True,
                        "credit_card_debt": "-300.00",
                        "void_retained": True,
                        "fresh_opening_id": True,
                        "original_receipt_unchanged": True,
                        "old_complete_reader_refused": True,
                        "fresh_process": reopened,
                        "installed_only": True,
                        "module_file": finance_core.__file__,
                    },
                    sort_keys=True,
                )
            )
        else:
            result = reader.collect_accounts()
            assert len(result.accounts) == 1
            effects = DailyLedgerReaderV1(ledger).collect_with_effects_v1()
            assert effects.collection.balances[0].balance_amount == "100.00"
            assert effects.effects[0].values["source"] == "map"
            assert effects.effects[0].values["map_owner_fact_ref"]["version"] == 5
            audit = ledger.reader().full_audit()
            from finance_core.instance_admission.authority import _logical_database_digest
            from finance_core.lifecycle_v2.reader import audit_evidence_connection

            conn = sqlite3.connect(
                (scratch / "snapshot.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                snapshot = audit_evidence_connection(conn, port, binding)
                assert (
                    _logical_database_digest(conn) == result.read_identity.logical_database_sha256
                )
            finally:
                conn.close()
            modules = [
                Path(m.__file__).resolve()
                for n, m in sys.modules.items()
                if (n == "finance_core" or n.startswith("finance_core."))
                and getattr(m, "__file__", None)
            ]
            assert modules and all(p.is_relative_to(installed.resolve()) for p in modules)
            assert not any(
                Path(p or ".").resolve().is_relative_to(checkout.resolve()) for p in sys.path
            )
            print(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "fact_versions": audit["fact_version_count"],
                        "snapshot_fact_versions": snapshot["fact_version_count"],
                        "opening_amount": result.accounts[0].opening_amount,
                        "logical_database_sha256": result.read_identity.logical_database_sha256,
                        "installed_only": True,
                    },
                    sort_keys=True,
                )
            )
    finally:
        if owner is not None and owner._lease.fd >= 0:
            owner.close()
        cfg.close()


if __name__ == "__main__":
    main()
