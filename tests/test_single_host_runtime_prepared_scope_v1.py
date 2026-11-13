"""Prepared-operation identity scope through callbacks and actual close."""

from dataclasses import replace

import pytest
from test_single_host_runtime_v1 import _prepared_test_service, _review_fix_rows

from finance_core.instance_admission.authority import STATE_FILENAME
from finance_core.lifecycle_v2 import LedgerService, LifecycleError


@pytest.fixture
def review_fix_offline_case(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from lifecycle_recovery_material_support_v1 import stage_expense

    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1

    config, _, owner, service, port, authenticator = _prepared_test_service(
        tmp_path, monkeypatch, instance_id="prepared-review-fix"
    )
    service.clock = lambda: 1792800000
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    try:
        _, _, review, _ = stage_expense(service, port, "review-fix", stage="delivery")
        port.register_action(service, review, "delivery-review-fix", "action-review-fix")
        yield SimpleNamespace(
            config=config,
            owner=owner,
            service=service,
            port=port,
            authenticator=authenticator,
            api=api,
            review=review,
            action="action-review-fix",
            operation="review-fix-finalize",
        )
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


@pytest.mark.parametrize("field", ["binding", "owner", "runtime_admission"])
@pytest.mark.parametrize(
    ("mode", "callback"),
    [("prepare", "action"), ("apply", "action"), ("apply", "late-clock"), ("cancel", "late-clock")],
)
def test_review_fix_prepared_callback_refuses_identity_drift_and_exact_retry(
    review_fix_offline_case, field, mode, callback
):
    case = review_fix_offline_case
    api, service = case.api, case.service
    reference = None
    if mode != "prepare":
        reference = api.prepare(case.review.review_id, case.action, case.operation)
    before = _review_fix_rows(case.owner)
    state_before = (case.config.control_root / STATE_FILENAME).read_bytes()
    original_binding = service.binding
    original_action, original_clock = case.port.action, service.clock
    changed = {
        "binding": replace(original_binding, environment="single-host-real"),
        "owner": None,
        "runtime_admission": object(),
    }[field]
    calls = []

    def mutate_action(conn, evidence_id, expected):
        value = original_action(conn, evidence_id, expected)
        calls.append("action")
        setattr(service, field, changed)
        return value

    def mutate_late_clock():
        calls.append("clock")
        if len(calls) == (2 if mode == "apply" else 1):
            setattr(service, field, changed)
        return 1792800000

    if callback == "action":
        case.port.action = mutate_action
    else:
        service.clock = mutate_late_clock
    try:
        with pytest.raises(LifecycleError, match="Runtime identity cannot change"):
            if mode == "prepare":
                api.prepare(case.review.review_id, case.action, case.operation)
            else:
                getattr(api, mode + "_offline")(
                    case.operation, expected_intent_sha256=reference.intent_sha256
                )
    finally:
        case.port.action = original_action
        service.clock = original_clock
    assert calls == (
        ["action"] if callback == "action" else ["clock"] * (2 if mode == "apply" else 1)
    )
    assert service.owner is case.owner and service.binding is original_binding
    assert service.runtime_admission is None and api.service is service
    assert _review_fix_rows(case.owner) == before
    assert (case.config.control_root / STATE_FILENAME).read_bytes() == state_before
    assert api.inspect(case.operation).state == (
        "ABSENT_LOCAL" if mode == "prepare" else "PREPARED_OFFLINE"
    )
    if mode == "prepare":
        retried = api.prepare(case.review.review_id, case.action, case.operation)
        assert api.prepare(case.review.review_id, case.action, case.operation) == retried
    else:
        retried = getattr(api, mode + "_offline")(
            case.operation, expected_intent_sha256=reference.intent_sha256
        )
        assert (
            getattr(api, mode + "_offline")(
                case.operation, expected_intent_sha256=reference.intent_sha256
            )
            == retried
        )
    after = _review_fix_rows(case.owner)
    assert len(after["facts"]) == int(mode == "apply")
    assert len(after["decisions"]) == int(mode == "apply")
    assert len(after["finalization_intents"]) == 1
    assert len(after["finalization_outcomes"]) == int(mode != "prepare")


@pytest.mark.parametrize(
    "entry",
    ["inspect", "material", "export_content_v1", "prepare", "apply_offline", "cancel_offline"],
)
def test_review_fix_six_prepared_entries_pin_service_and_identity_through_actual_close(
    review_fix_offline_case, monkeypatch, entry
):
    from finance_core.instance_admission import sessions

    case = review_fix_offline_case
    api, service = case.api, case.service
    reference = api.prepare(case.review.review_id, case.action, case.operation)
    before = _review_fix_rows(case.owner)
    original_binding = service.binding
    redirected = LedgerService(
        case.owner, case.port, replace(original_binding, environment="single-host-real")
    )
    original_verify = case.authenticator.verify
    original_close = sessions._close_sqlite_handle
    observed = []

    def attempt_mutations(stage):
        changes = (
            (service, "binding", redirected.binding),
            (service, "owner", None),
            (service, "runtime_admission", object()),
        )
        for target, field, value in changes:
            try:
                setattr(target, field, value)
            except LifecycleError:
                outcome = "REFUSED"
            else:
                outcome = "UNEXPECTED_MUTATION"
            observed.append((stage, field, outcome))
        # Redirecting the public pointer is allowed, but the current operation
        # must continue using its captured original service through actual close.
        api.service = redirected

    def verify_with_mutation(**kwargs):
        original_verify(**kwargs)
        attempt_mutations("authenticator")

    def close_with_mutation(conn):
        # Record instead of asserting from actual close: an assertion here
        # would itself create an unrelated UNKNOWN connection lifetime.
        attempt_mutations("actual-close")
        observed.append(
            (
                "actual-close",
                "connection-owner",
                "ORIGINAL" if case.owner._active.connection is conn else "WRONG_OWNER",
            )
        )
        original_close(conn)

    with monkeypatch.context() as callbacks:
        callbacks.setattr(case.authenticator, "verify", verify_with_mutation)
        callbacks.setattr(sessions, "_close_sqlite_handle", close_with_mutation)
        if entry == "inspect":
            assert api.inspect(case.operation).state == "PREPARED_OFFLINE"
        elif entry == "prepare":
            assert api.prepare(case.review.review_id, case.action, case.operation) == reference
        else:
            result = getattr(api, entry)(
                case.operation, expected_intent_sha256=reference.intent_sha256
            )
            if entry == "material":
                assert result
            elif entry == "export_content_v1":
                assert result.anchor.intent_sha256 == reference.intent_sha256
            elif entry == "apply_offline":
                assert result.status == "LOCAL_POSTED"
            else:
                assert result.state == "CANCELLED_OFFLINE"
    assert {stage for stage, _, _ in observed} == {"authenticator", "actual-close"}
    assert all(
        outcome == ("ORIGINAL" if field == "connection-owner" else "REFUSED")
        for _, field, outcome in observed
    )
    assert api.service is redirected
    assert service.owner is case.owner and service.binding is original_binding
    assert service.runtime_admission is None
    if entry not in {"apply_offline", "cancel_offline"}:
        assert _review_fix_rows(case.owner) == before
    # The redirected pointer applies only to a subsequent call. Its external
    # scope must be refused until the original service is explicitly restored.
    with pytest.raises(LifecycleError, match="offline-synthetic"):
        api.inspect(case.operation)
    api.service = service
    assert api.inspect(case.operation).state in {
        "PREPARED_OFFLINE",
        "APPLIED_OFFLINE",
        "CANCELLED_OFFLINE",
    }
