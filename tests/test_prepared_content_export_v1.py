"""Protected actual-source export and detached B1 intent association proofs."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from prepared_envelope_test_support import native_rows, prepared_case

from finance_core.instance_admission import InstanceAdmissionError
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError, RuntimeBinding
from finance_core.lifecycle_v2._prepared_content_v1 import _check_prepared_content_v1
from finance_core.lifecycle_v2.contracts import canonical


def exported(case):
    return case.preparation.export_content_v1(
        case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
    )


def test_export_retains_original_body_and_blobs_after_actual_close(tmp_path, monkeypatch):
    import finance_core.lifecycle_v2.prepared_finalization_v1 as preparation_module

    with prepared_case(tmp_path, monkeypatch) as case:
        before = native_rows(case.service)
        with lifecycle_read_operation(case.service.owner) as conn:
            body, intent = conn.execute(
                "SELECT body_json,intent_sha256 FROM l2_finalization_intents"
            ).fetchone()
            original = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT package_sha256,payload FROM l2_finalization_materials"
                )
            }
        original_read = preparation_module.lifecycle_read_operation
        operations = []

        @contextmanager
        def traced_read(owner):
            with original_read(owner) as conn:
                operations.append(conn)
                yield conn
                assert not conn.in_transaction

        monkeypatch.setattr(preparation_module, "lifecycle_read_operation", traced_read)
        content = exported(case)
        assert len(operations) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(operations[0], "SELECT 1")
        assert case.service.owner._active is None
        assert content.intent_body == body.encode()
        assert hashlib.sha256(content.intent_body).hexdigest() == intent
        assert content.packages == tuple(
            original[item.package_sha256] for item in content.anchor.packages
        )
        assert content.anchor.operation_id == case.reference.operation_id
        assert content.anchor.intent_sha256 == case.reference.intent_sha256
        assert content.observed_local_state == "PREPARED_OFFLINE"
        assert content.content_kind == "PREPARED_PROJECTION"
        assert native_rows(case.service) == before
        checked = _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=case.authenticator,
        )
        assert checked.frontier == content.anchor.projected_frontier
        assert checked.current_values[0][0] == case.reference.record_id
        with pytest.raises(InstanceAdmissionError, match="Pending exact finalization"):
            case.service.capture("missing", "pending-ordinary-write")


@pytest.mark.parametrize("state", ["APPLIED_OFFLINE", "CANCELLED_OFFLINE", "EXPIRED"])
def test_terminal_and_expired_exports_are_only_historical_projection(tmp_path, monkeypatch, state):
    with prepared_case(tmp_path, monkeypatch) as case:
        original = exported(case)
        if state == "APPLIED_OFFLINE":
            case.preparation.apply_offline(
                case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
            )
        elif state == "CANCELLED_OFFLINE":
            case.preparation.cancel_offline(
                case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
            )
        else:
            case.service.clock = lambda: 1792800600
        content = exported(case)
        assert content.intent_body == original.intent_body
        assert content.packages == original.packages
        assert content.anchor == original.anchor
        assert content.content_kind == "PREPARED_PROJECTION"
        assert content.observed_local_state == ("PREPARED_OFFLINE" if state == "EXPIRED" else state)
        _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=case.authenticator,
        )
        if state == "EXPIRED":
            with pytest.raises(LifecycleError):
                case.preparation.apply_offline(
                    case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
                )
            assert case.preparation.inspect(case.reference.operation_id).state == "PREPARED_OFFLINE"


def test_export_wrong_reference_binding_and_current_evidence_refuse(tmp_path, monkeypatch):
    with prepared_case(tmp_path, monkeypatch) as case:
        with pytest.raises(LifecycleError):
            case.preparation.export_content_v1(
                "absent", expected_intent_sha256=case.reference.intent_sha256
            )
        with pytest.raises(LifecycleError):
            case.preparation.export_content_v1(
                case.reference.operation_id, expected_intent_sha256="0" * 64
            )
        original = case.service.binding
        case.service.binding = RuntimeBinding(
            original.instance_id, "wrong-generation", "user", "chat"
        )
        with pytest.raises(LifecycleError):
            exported(case)
        case.service.binding = original
        case.port.disabled = True
        with pytest.raises((LifecycleError, AssertionError)):
            exported(case)
        case.port.disabled = False
        assert exported(case).anchor.intent_sha256 == case.reference.intent_sha256


@pytest.fixture(scope="module")
def detached_content(tmp_path_factory):
    root = tmp_path_factory.mktemp("detached-prepared-content")
    environment = pytest.MonkeyPatch()
    try:
        with prepared_case(root, environment) as case:
            content = exported(case)
            source = Path(case.config._binding["profile_root"]) / "runtime/database/finance.db"
            case.port.destroy()
            case.service.owner.close()
            source.unlink()
            assert not source.exists()
            yield content, case.authenticator
    finally:
        environment.undo()


@pytest.mark.parametrize(
    "damage", ["body", "package", "identity", "operation", "frontier", "size", "order"]
)
def test_detached_fixed_anchor_rejects_changed_material(detached_content, damage):
    content, authenticator = detached_content
    body, packages, anchor = content.intent_body, content.packages, content.anchor
    if damage == "body":
        body += b" "
    elif damage == "package":
        packages = (packages[0] + b" ",)
    elif damage == "identity":
        anchor = replace(
            anchor, source_identity=replace(anchor.source_identity, core_identity="other")
        )
    elif damage == "operation":
        anchor = replace(anchor, operation_id="other")
    elif damage == "frontier":
        anchor = replace(
            anchor, projected_frontier=replace(anchor.projected_frontier, event_sha256="0" * 64)
        )
    elif damage == "size":
        anchor = replace(
            anchor,
            packages=(replace(anchor.packages[0], size_bytes=anchor.packages[0].size_bytes + 1),),
        )
    else:
        packages += packages
    with pytest.raises(LifecycleError):
        _check_prepared_content_v1(body, packages, expected=anchor, authenticator=authenticator)


@pytest.mark.parametrize(
    "field", ["expires_at", "review_id", "action_evidence_id", "binding", "before", "append"]
)
def test_detached_reuses_original_journal_association_rules(detached_content, field):
    import json

    content, authenticator = detached_content
    body = json.loads(content.intent_body)
    if field == "expires_at":
        body[field] += 1
    elif field in {"review_id", "action_evidence_id"}:
        body[field] = "other"
    elif field == "binding":
        body[field]["generation"] = "other"
    elif field == "before":
        body[field]["rows_sha256"] = "0" * 64
    else:
        body[field]["fact"]["fact_sha256"] = "0" * 64
    changed = canonical(body).encode()
    anchor = replace(content.anchor, intent_sha256=hashlib.sha256(changed).hexdigest())
    with pytest.raises(LifecycleError):
        _check_prepared_content_v1(
            changed, content.packages, expected=anchor, authenticator=authenticator
        )


def test_detached_requires_external_evidence_root_and_no_original_source(detached_content):
    from lifecycle_recovery_material_support_v1 import SyntheticAuthenticatorV1

    content, authenticator = detached_content
    with pytest.raises(LifecycleError):
        _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=SyntheticAuthenticatorV1(b"wrong-external-root"),
        )
    checked = _check_prepared_content_v1(
        content.intent_body,
        content.packages,
        expected=content.anchor,
        authenticator=authenticator,
    )
    assert asdict(checked.identity) == asdict(content.anchor.source_identity)
    assert checked.frontier == content.anchor.projected_frontier


@pytest.mark.parametrize("fault", ["close-before", "close-after", "cursor-close", "rollback"])
def test_export_actual_close_or_rollback_unknown_never_releases_content(tmp_path, fault):
    import os
    import subprocess
    import sys

    script = r"""
import os, sqlite3, sys
from pathlib import Path
from prepared_envelope_test_support import prepared_case
from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
import finance_core.instance_admission.sessions as sessions
class Environment:
    def setenv(self, key, value): os.environ[key] = value
with prepared_case(Path(sys.argv[1]), Environment()) as case:
    original_close = sessions._close_sqlite_handle
    fault = sys.argv[2]
    def fail_close(conn):
        if fault == 'close-after': original_close(conn)
        raise sqlite3.OperationalError('injected actual close failure')
    def fail(*args): raise sqlite3.OperationalError('injected actual cleanup failure')
    if fault.startswith('close-'): sessions._close_sqlite_handle = fail_close
    elif fault == 'cursor-close': sessions._close_cursor_handle = fail
    else:
        sessions._ProductionConnection.rollback = fail
        sessions._rollback_sqlite_handle = fail
    try:
        case.preparation.export_content_v1(case.reference.operation_id,
            expected_intent_sha256=case.reference.intent_sha256)
    except (InstanceAdmissionError, sqlite3.Error): pass
    else: raise AssertionError('Content escaped uncertain actual close/rollback')
    assert case.service.owner._uncertain
    for action in (case.service.owner.close,
                   lambda: acquire_production_owner(case.config, timeout_seconds=0)):
        try: action()
        except InstanceAdmissionError: pass
        else: raise AssertionError('Uncertain source custody was released')
    os._exit(0)
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), fault],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "tests"), str(root)))),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_detached_scratch_actual_close_failure_never_returns_content(detached_content, monkeypatch):
    import finance_core.lifecycle_v2._prepared_content_v1 as module

    content, authenticator = detached_content
    original = sqlite3.connect
    connections = []

    class CloseFailure(sqlite3.Connection):
        def close(self):
            super().close()
            raise sqlite3.OperationalError("injected scratch actual close failure")

    def connect(*args, **kwargs):
        # First connection belongs to the unchanged authenticating native check;
        # second is the detached association scratch DB owned by this module.
        kwargs["factory"] = CloseFailure if connections else sqlite3.Connection
        conn = original(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="scratch actual close"):
        _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=authenticator,
        )
    assert len(connections) == 2
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, "SELECT 1")


def test_export_retains_complete_incremental_chain(tmp_path, monkeypatch):
    from lifecycle_recovery_material_support_v1 import make_service, stage_expense

    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
    from finance_core.lifecycle_v2.recovery_material_v2 import collect_native_closure_v2

    service, port, config, authenticator = make_service(tmp_path, monkeypatch)
    try:
        stage_expense(service, port, "first")
        first = collect_native_closure_v2(service, archive_source=port, authenticator=authenticator)
        _, _, review, _ = stage_expense(service, port, "second", stage="delivery")
        port.register_action(service, review, "delivery-second", "action-second")
        preparation = PreparedFinalizationServiceV1(
            service, archive_source=port, authenticator=authenticator
        )
        reference = preparation.prepare(
            review.review_id, "action-second", "finalize-second", previous_packages=(first,)
        )
        before = native_rows(service)
        content = preparation.export_content_v1(
            reference.operation_id, expected_intent_sha256=reference.intent_sha256
        )
        assert len(content.packages) == 2
        assert content.packages[0] == first
        assert content.packages == preparation.material(
            reference.operation_id, expected_intent_sha256=reference.intent_sha256
        )
        checked = _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=authenticator,
        )
        assert checked.inventory.fact_version_count == 2
        assert native_rows(service) == before
        assert len(before["facts"]) == 1
    finally:
        service.owner.close()
        config.close()
