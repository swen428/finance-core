"""Independent retained evidence for daily Core ledgers without app tables."""

from dataclasses import replace

import pytest
from test_lifecycle_v2_round3_recovery import _fence, _manual_historical_target
from test_lifecycle_v2_round3_recovery import original as original

from finance_core.instance_admission import (
    acquire_production_owner,
    fence_production_instance,
    inspect_instance_admission,
)
from finance_core.lifecycle_v2 import LedgerService, LifecycleError
from finance_core.lifecycle_v2.core_archive_origin_v1 import (
    core_archive_origin_approval_verifier_v1,
    issue_core_archive_origin_v1,
)
from finance_core.lifecycle_v2.daily_v1 import DailyAccountReaderV1, DailyAccountServiceV1
from finance_core.lifecycle_v2.historical_evidence_v1 import (
    HistoricalEvidenceV1,
    HistoricalGenerationV1,
)
from finance_core.lifecycle_v2.recovery import (
    RecoveryAuthority,
    promote_manual_single_host_restore_v1,
)
from tests.daily_account_support_v1 import NOW, DailyPort
from tests.test_daily_accounts_lifecycle_v1 import create


@pytest.mark.parametrize("empty_source", [False, True])
def test_core_only_daily_restore_uses_original_evidence_on_two_successive_targets(
    original, tmp_path, monkeypatch, empty_source
):
    source, owner, original_service, _ = original
    old_port = original_service.port = DailyPort()
    original_service.clock = lambda: NOW
    authority = RecoveryAuthority(
        "core-only-archive-test", b"independent synthetic archive signing root"
    )
    accepts_origin = core_archive_origin_approval_verifier_v1(authority)

    def approve(reference, action, checksum):
        return (
            accepts_origin(reference, action, checksum)
            if action == "core_archive_origin_v1"
            else reference == "approved"
        )

    accounts = []
    if not empty_source:
        _, _, _, first = create(original_service, old_port, DailyAccountServiceV1(original_service))
        accounts.append(first.account_ref.account_id)
    snapshot = tmp_path / "original.sqlite"
    audit = original_service.reader().full_audit()
    cut = original_service.seal_snapshot(snapshot)
    original_history = HistoricalGenerationV1(
        original_service.binding, cut, None, audit["audit_sha256"]
    )
    origin = issue_core_archive_origin_v1(original_service, cut, authority, snapshot_path=snapshot)
    original_evidence = HistoricalEvidenceV1(original_history, old_port)
    _fence(source, owner)
    target, held, grants = _manual_historical_target(
        tmp_path, monkeypatch, source, cut, snapshot, "successor", NOW, verifier=approve
    )
    with target:
        with pytest.raises(LifecycleError, match="independently signed source"):
            promote_manual_single_host_restore_v1(
                held, **grants, historical_evidence=(original_evidence,)
            )
        if not empty_source:
            wrong_anchor = replace(original_history, event_sha256="f" * 64)
            with pytest.raises(LifecycleError, match="event"):
                promote_manual_single_host_restore_v1(
                    held,
                    **grants,
                    core_archive_origin=origin,
                    historical_evidence=(HistoricalEvidenceV1(wrong_anchor, old_port),),
                )
        assert inspect_instance_admission(target)["state"] == "RESTORE_HELD"
        active = promote_manual_single_host_restore_v1(
            held, **grants, core_archive_origin=origin, historical_evidence=(original_evidence,)
        )
        assert active["state"] == "ACTIVE"
        next_port = DailyPort()
        next_port.sources.clear()
        next_port.add_source("source2", "Create another MYR bank account")
        with acquire_production_owner(target) as new_owner:
            current = LedgerService(
                new_owner,
                next_port,
                grants["binding"],
                clock=lambda: NOW,
                runtime_admission=grants["runtime_admission"],
                historical_evidence=(original_evidence,),
            )
            for account_id in accounts:
                assert (
                    DailyAccountReaderV1(current).get_account(account_id).opening_amount == "12.30"
                )
            _, _, _, second = create(current, next_port, DailyAccountServiceV1(current), suffix="2")
            next_snapshot = tmp_path / "successor.sqlite"
            next_audit = current.reader().full_audit()
            next_cut = current.seal_snapshot(next_snapshot)
            next_origin = issue_core_archive_origin_v1(
                current, next_cut, authority, snapshot_path=next_snapshot
            )
            accounts.append(second.account_ref.account_id)
        assert (
            promote_manual_single_host_restore_v1(
                held, **grants, core_archive_origin=origin, historical_evidence=(original_evidence,)
            )
            == active
        )
        current_history = HistoricalGenerationV1(
            grants["binding"], next_cut, None, next_audit["audit_sha256"]
        )
        chain = (original_evidence, HistoricalEvidenceV1(current_history, next_port))
        fence_production_instance(target, operation_id="fence", approval_ref="approved")
        third, held_again, grants_again = _manual_historical_target(
            tmp_path,
            monkeypatch,
            target,
            next_cut,
            next_snapshot,
            "next-successor",
            NOW,
            verifier=approve,
        )
        with third:
            with pytest.raises(LifecycleError, match="selected archive"):
                promote_manual_single_host_restore_v1(
                    held_again,
                    **grants_again,
                    historical_generations=(original_history, current_history),
                    historical_evidence=chain[1:],
                )
            # Even an empty ancestor has a source-issued parent commitment.
            with pytest.raises(LifecycleError, match="independently signed source"):
                promote_manual_single_host_restore_v1(
                    held_again,
                    **grants_again,
                    core_archive_origin=next_origin,
                    historical_evidence=chain[1:],
                )
            with pytest.raises(LifecycleError):
                promote_manual_single_host_restore_v1(
                    held_again,
                    **grants_again,
                    core_archive_origin=origin,
                    historical_evidence=chain,
                )
            assert inspect_instance_admission(third)["state"] == "RESTORE_HELD"
            assert (
                promote_manual_single_host_restore_v1(
                    held_again,
                    **grants_again,
                    core_archive_origin=next_origin,
                    historical_evidence=chain,
                )["state"]
                == "ACTIVE"
            )
            with acquire_production_owner(third) as last_owner:
                ledger = LedgerService(
                    last_owner,
                    DailyPort(),
                    grants_again["binding"],
                    clock=lambda: NOW,
                    runtime_admission=grants_again["runtime_admission"],
                    historical_evidence=chain,
                )
                assert ledger.reader().full_audit() == next_audit
                assert all(
                    DailyAccountReaderV1(ledger).get_account(identity).opening_amount == "12.30"
                    for identity in accounts
                )
