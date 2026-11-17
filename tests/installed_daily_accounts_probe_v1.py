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
    from daily_account_support_v1 import NOW, DailyPort, command, unknown_time

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
        VerifiedDailyProposalV1,
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
        port = DailyPort()
        state_path = scratch / "trusted-synthetic-port.json"
        if mode == "readback":
            state = json.loads(state_path.read_text())
            for key, cls in (
                ("sources", VerifiedSource),
                ("observations", VerifiedDailyProposalV1),
                ("deliveries", VerifiedDelivery),
                ("actions", VerifiedAction),
            ):
                setattr(port, key, {k: cls(**v) for k, v in state[key].items()})
            port.commands = state["commands"]
        binding = RuntimeBinding(cfg.instance_id, "daily-generation-1", "user", "chat")
        ledger = LedgerService(owner, port, binding, clock=lambda: NOW)
        daily = DailyAccountServiceV1(ledger)
        reader = DailyAccountReaderV1(ledger)
        if mode == "flow":
            port.add_source(
                "source1", "Create my MYR credit card account; opening not yet specified"
            )
            source = ledger.capture("source1", "capture1")
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

            def change(kind, fields, identity, reason=None):
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
            cut = ledger.seal_snapshot(scratch / "snapshot.sqlite")
            state = {
                key: {k: asdict(v) for k, v in getattr(port, key).items()}
                for key in ("sources", "observations", "deliveries", "actions")
            }
            state["commands"] = port.commands
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
            assert reopened["fact_versions"] == 4 and reopened["snapshot_fact_versions"] == 4
            assert (
                reopened["logical_database_sha256"]
                == before["logical_database_sha256"]
                == cut.logical_database_sha256
            )
            assert reopened["installed_only"] and reopened["opening_amount"] == "0.00"
            print(
                json.dumps(
                    {
                        "contract": DAILY_CONTRACT_VERSION,
                        "fact_versions": 4,
                        "proposal_count": 5,
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
