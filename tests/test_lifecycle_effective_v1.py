"""Real-owner synthetic proof for complete effective native ledger reads."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date
from decimal import localcontext
from types import SimpleNamespace

import pytest
from test_lifecycle_v2 import _adversarial_database, _correct, _rehash_audit, ready, response
from test_lifecycle_v2 import live as live

from finance_core.instance_admission import InstanceAdmissionError
from finance_core.lifecycle_v2 import VerifiedSource
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.effective_v1 import (
    MAX_COMPLETE_FACT_VERSIONS_V1,
    MAX_COMPLETE_RECORDS_V1,
    EffectiveFactV1,
    EffectiveFilterV1,
    EffectiveLedgerReaderV1,
    EffectiveReadError,
    FactVersionV1,
    ReadLimitsV1,
)


def _post(service, port, key, *, amount="7.25", currency="USD", day="2026-10-25", extra=False):
    text = f"{day} Shop {currency} {amount}"
    fields = {"amount": amount, "currency": currency, "merchant": "Shop", "transaction_date": day}
    if extra:
        text += "; category: Food; description: Lunch"
        fields.update(category="Food", description="Lunch")
    port.sources[key] = VerifiedSource(
        key,
        "user",
        "chat",
        "message-" + key,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )
    raw = json.loads(response(text, fields=fields))
    day_value = date.fromisoformat(day)
    for operation in raw["operations"]:
        if operation["field"] == "transaction_date":
            operation["date_expression"] = {
                "kind": "absolute_date",
                "year": day_value.year,
                "month": day_value.month,
                "day": day_value.day,
            }
    port.raw = json.dumps(raw)
    source = service.capture(key, "capture-" + key)
    proposal = service.propose(source.source_id, "obs-" + key, 0, "propose-" + key)
    review = service.prepare_review(proposal.proposal_id, proposal.version)
    service.record_delivery(review.review_id, "delivery-" + key)
    receipt = service.confirm(review.review_id, "action-" + key, "confirm-" + key)
    return source, review, receipt


def _first(service):
    source, _, review = ready(service)
    return source, review, service.confirm(review.review_id, "action1", "confirm1")


def _refused(code, operation):
    with pytest.raises(EffectiveReadError) as error:
        operation()
    assert error.value.code == code


def test_empty_read_missing_record_and_read_identity(live):
    service, _, _ = live
    reader = EffectiveLedgerReaderV1(service)
    empty = reader.collect()
    assert empty.facts == () and empty.complete is True
    assert empty.scope == "native-lifecycle-v2"
    assert empty.read_identity.event_seq == 0
    assert empty.read_identity.event_sha256 is None
    assert empty.read_identity.runtime_binding_sha256 == digest(asdict(service.binding))
    assert empty.read_identity.config_binding_sha256 == service.owner.config.binding_sha256
    assert reader.get("missing") is None
    assert reader.history("missing") is None
    assert reader.summary().items == ()
    assert json.loads(reader.export_json())["facts"] == []


def test_current_history_source_summary_export_and_replay(live):
    service, port, _ = live
    source, review, first = _first(service)
    _, corrected = _correct(service, port, first)
    _, _, usd = _post(service, port, "usd", extra=True)
    _, _, jpy = _post(service, port, "jpy", currency="JPY", amount="100", day="2026-10-23")
    reader = EffectiveLedgerReaderV1(service)
    assert service.confirm(review.review_id, "action1", "confirm1") == first
    current = reader.get(first.record_id)
    assert type(current) is EffectiveFactV1
    assert (current.version, current.amount, current.currency) == (2, "15.00", "SGD")
    assert current.economic_event_id == "message-1"
    assert current.fact_sha256 == corrected.fact_sha256
    assert current.commit_seq == corrected.commit_seq
    assert current.source.source_id == source.source_id
    assert current.source.evidence_id == "source1"
    assert current.source.source_sha256 == source.source_sha256
    assert current.account_status == "unknown" and current.account_id is None
    assert current.status == "LOCAL_POSTED"
    with pytest.raises(FrozenInstanceError):
        current.amount = "100.00"
    history = reader.history(first.record_id)
    assert history.current_version == 2
    assert [type(version) for version in history.versions] == [FactVersionV1, FactVersionV1]
    assert [(v.version, v.amount) for v in history.versions] == [(1, "12.00"), (2, "15.00")]
    assert all(v.economic_event_id == current.economic_event_id for v in history.versions)
    assert history.versions[0].fact_sha256 == first.fact_sha256
    assert reader.source(source.source_id) == port.sources["source1"]
    result = reader.collect()
    assert [fact.record_id for fact in result.facts] == [
        jpy.record_id,
        first.record_id,
        usd.record_id,
    ]
    assert result.facts[-1].category == "Food"
    assert result.facts[-1].description == "Lunch"
    summary = reader.summary()
    assert [(item.currency, item.event_count, item.total_amount) for item in summary.items] == [
        ("JPY", 1, "100"),
        ("SGD", 1, "15.00"),
        ("USD", 1, "7.25"),
    ]
    assert summary.read_identity == result.read_identity
    exported = reader.export_json()
    assert exported == canonical(asdict(result))
    assert "raw_input" not in exported and "attachment_path" not in exported
    assert "grand_total" not in asdict(summary)


def test_filters_are_inclusive_normalized_and_represented_in_every_output(live):
    service, port, _ = live
    _, _, first = _first(service)
    _post(service, port, "same-day", currency="SGD", amount="0.25", day="2026-10-24")
    _post(service, port, "later")
    reader = EffectiveLedgerReaderV1(service)
    filters = EffectiveFilterV1("2026-10-24", "2026-10-24", " sgd ")
    result = reader.collect(filters)
    assert len(result.facts) == 2
    assert first.record_id in {fact.record_id for fact in result.facts}
    assert list(result.facts) == sorted(
        result.facts, key=lambda f: (f.transaction_date, f.record_id)
    )
    assert result.filters.currency == "SGD"
    summary = reader.summary(filters)
    assert summary.filters == filters and summary.items[0].total_amount == "12.25"
    assert json.loads(reader.export_json(filters))["filters"] == asdict(filters)
    assert reader.collect(EffectiveFilterV1(date_from="2026-10-26")).facts == ()


def test_summary_integer_minor_units_ignores_ambient_decimal_precision(live):
    service, port, _ = live
    _first(service)
    _post(service, port, "sgd2", currency="SGD", amount="0.25")
    with localcontext() as context:
        context.prec = 2
        summary = EffectiveLedgerReaderV1(service).summary()
    assert [(item.event_count, item.total_amount) for item in summary.items] == [(2, "12.25")]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"date_from": "20261024"},
        {"date_to": "2026-02-30"},
        {"date_from": True},
        {"date_from": "2026-10-25", "date_to": "2026-10-24"},
        {"currency": "XYZ"},
        {"currency": True},
    ],
)
def test_strict_filters(kwargs):
    _refused("invalid_read_request", lambda: EffectiveFilterV1(**kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_records": True},
        {"max_records": 0},
        {"max_records": 1.0},
        {"max_records": MAX_COMPLETE_RECORDS_V1 + 1},
        {"max_fact_versions": MAX_COMPLETE_FACT_VERSIONS_V1 + 1},
        {"max_fact_versions": False},
    ],
)
def test_strict_bounded_limits(kwargs):
    _refused("invalid_read_request", lambda: ReadLimitsV1(**kwargs))


def test_complete_caps_apply_before_filters_and_do_not_truncate(live, monkeypatch):
    from finance_core.lifecycle_v2.reader import LedgerReader

    service, port, _ = live
    _, _, posted = _first(service)
    _correct(service, port, posted)
    _post(service, port, "second")
    reader = EffectiveLedgerReaderV1(service)
    filters = EffectiveFilterV1(date_from="2026-12-31")
    for limits in (ReadLimitsV1(max_records=1), ReadLimitsV1(max_fact_versions=2)):
        for action in (reader.collect, reader.summary, reader.export_json):
            _refused("complete_read_limit_exceeded", lambda: action(filters, limits))
        _refused("complete_read_limit_exceeded", lambda: reader.history(posted.record_id, limits))
    monkeypatch.setattr(LedgerReader, "recent", lambda *args: pytest.fail("recent page used"))
    assert len(reader.collect(limits=ReadLimitsV1(2, 3)).facts) == 2
    for value in ({}, [], True):
        _refused("invalid_read_request", lambda: reader.collect(value))
    _refused("invalid_read_request", lambda: reader.collect(limits={}))
    for identity in ("", None, True):
        _refused("invalid_read_request", lambda: reader.get(identity))


@pytest.mark.parametrize("damage", ["all-facts", "fact-and-decision", "latest", "event"])
def test_reverse_inventory_catches_missing_whole_record_and_versions(live, damage):
    service, port, _ = live
    _, _, first = _first(service)
    _, corrected = _correct(service, port, first)
    with _adversarial_database(service) as conn:
        if damage == "all-facts":
            conn.execute("DELETE FROM l2_facts")
        elif damage == "fact-and-decision":
            conn.execute("DELETE FROM l2_decisions")
            conn.execute("DELETE FROM l2_facts")
        elif damage == "latest":
            conn.execute("DELETE FROM l2_facts WHERE version=2")
        else:
            conn.execute(
                "UPDATE l2_events SET entity_id='forged' WHERE seq=?", (corrected.commit_seq,)
            )
            _rehash_audit(conn)
    reader = EffectiveLedgerReaderV1(service)
    for operation in (
        reader.collect,
        reader.summary,
        reader.export_json,
        lambda: reader.get(first.record_id),
        lambda: reader.history(first.record_id),
    ):
        _refused("fact_evidence_invalid", operation)


@pytest.mark.parametrize("damage", ["source", "provider", "delivery", "human", "old-fact"])
def test_tamper_outside_filter_and_old_version_is_refused(live, damage):
    service, port, _ = live
    _, _, first = _first(service)
    _correct(service, port, first)
    _post(service, port, "usd")
    if damage == "source":
        port.sources["source1"] = replace(port.sources["source1"], raw_input="changed")
    elif damage == "provider":
        port.observations["obs1"] = replace(port.observations["obs1"], raw_response="{}")
    elif damage == "human":
        port.bad_actor = True
    elif damage == "delivery":
        original = port.delivery
        port.delivery = lambda *args: replace(original(*args), display_sha256="0" * 64)
    else:
        with _adversarial_database(service) as conn:
            conn.execute(
                "UPDATE l2_facts SET fact_sha256=? WHERE version=1 AND record_id=?",
                ("0" * 64, first.record_id),
            )
    reader = EffectiveLedgerReaderV1(service)
    filters = EffectiveFilterV1(currency="USD")
    for action in (reader.collect, reader.summary, reader.export_json):
        _refused("fact_evidence_invalid", lambda: action(filters))


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "12.0"),
        ("currency", "sgd"),
        ("transaction_type", "transfer"),
        ("description", []),
        ("category", ""),
        ("extra", "unsupported"),
    ],
)
def test_unsupported_fact_shape_refused_before_filter(live, field, value):
    service, _, _ = live
    _first(service)
    with _adversarial_database(service) as conn:
        row = conn.execute("SELECT values_json FROM l2_facts").fetchone()
        values = json.loads(row[0])
        values[field] = value
        conn.execute("UPDATE l2_facts SET values_json=?", (canonical(values),))
    _refused(
        "unsupported_fact_shape",
        lambda: EffectiveLedgerReaderV1(service).collect(EffectiveFilterV1(currency="USD")),
    )


@pytest.mark.parametrize("scope", ["import-row", "import-event", "legacy-source"])
def test_imported_scope_explicitly_refused(live, scope):
    service, _, _ = live
    _first(service)
    with _adversarial_database(service) as conn:
        if scope == "import-row":
            conn.execute("INSERT INTO l2_imports VALUES('unsupported',?,'{}')", ("0" * 64,))
        elif scope == "import-event":
            conn.execute("UPDATE l2_events SET kind='import' WHERE kind='confirm'")
        else:
            conn.execute("UPDATE l2_sources SET source_key='legacy:unsupported'")
    _refused("unsupported_ledger_scope", EffectiveLedgerReaderV1(service).collect)


def test_every_evidence_and_projection_share_explicit_transaction_and_close(live, monkeypatch):
    import finance_core.lifecycle_v2.effective_v1 as effective

    service, port, _ = live
    source, _, _ = _first(service)
    real_source = port.source
    connections = set()
    events = []

    def observe(conn, identity):
        assert conn.in_transaction
        connections.add(id(conn))
        events.append("evidence")
        return real_source(conn, identity)

    port.source = observe
    operation = effective.lifecycle_read_operation

    @contextmanager
    def observed(owner):
        with operation(owner) as conn:
            yield conn
            assert not conn.in_transaction
            events.append("rolled-back")
        assert owner._active is None
        events.append("closed")

    monkeypatch.setattr(effective, "lifecycle_read_operation", observed)
    result = effective.EffectiveLedgerReaderV1(service).collect()
    assert len(connections) == 1 and len(result.facts) == 1
    assert events[-2:] == ["rolled-back", "closed"]
    assert (
        effective.EffectiveLedgerReaderV1(service).source(source.source_id)
        == port.sources["source1"]
    )


def test_ordinary_cleanup_refusal_never_returns_result_and_owner_recovers(live, monkeypatch):
    import finance_core.lifecycle_v2.effective_v1 as effective

    service, _, _ = live
    _first(service)
    operation = effective.lifecycle_read_operation

    @contextmanager
    def cleanup_refusal(owner):
        with operation(owner) as conn:
            yield conn
        raise InstanceAdmissionError("synthetic cleanup refusal")

    with monkeypatch.context() as patch:
        patch.setattr(effective, "lifecycle_read_operation", cleanup_refusal)
        with pytest.raises(InstanceAdmissionError, match="cleanup refusal"):
            effective.EffectiveLedgerReaderV1(service).collect()
    assert service.owner._active is None
    assert len(effective.EffectiveLedgerReaderV1(service).collect().facts) == 1


@pytest.mark.parametrize("intent", ["income", "transfer", "refund"])
def test_unsupported_financial_intent_cannot_create_fact(live, intent):
    from finance_core.lifecycle_v2 import LifecycleError

    service, port, _ = live
    source = service.capture("source1", "capture1")
    raw = json.loads(port.raw)
    raw["intent"] = intent
    port.raw = json.dumps(raw)
    with pytest.raises(LifecycleError, match="not complete"):
        service.propose(source.source_id, "unsupported-observation", 0, "unsupported-proposal")
    reader = EffectiveLedgerReaderV1(service)
    assert reader.collect().facts == ()
    assert reader.summary().items == ()


def test_unconfirmed_review_remains_outside_effective_events(live):
    service, _, _ = live
    ready(service)
    result = EffectiveLedgerReaderV1(service).collect()
    assert result.facts == ()
    assert result.read_identity.event_seq == 4
    assert result.complete is True


def test_reader_requires_actual_service_owner_and_matching_binding(live):
    service, _, _ = live
    fake = SimpleNamespace(owner=service.owner, binding=service.binding, port=service.port)
    _refused("invalid_read_request", lambda: EffectiveLedgerReaderV1(fake))
    original = service.binding
    try:
        service.binding = replace(original, instance_id="other-instance")
        _refused("invalid_read_request", lambda: EffectiveLedgerReaderV1(service))
    finally:
        service.binding = original
    assert EffectiveLedgerReaderV1(service).collect().facts == ()


def test_native_uppercase_source_key_is_not_reserved_legacy_prefix(live):
    service, port, _ = live
    port.sources["source1"] = replace(port.sources["source1"], source_key="LEGACY:upper")
    _, _, posted = _first(service)
    fact = EffectiveLedgerReaderV1(service).get(posted.record_id)
    assert fact.economic_event_id == "LEGACY:upper"
    assert fact.source.source_key == "LEGACY:upper"


def test_actual_close_unknown_releases_no_collection_and_keeps_owner_held(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import os
import sqlite3
import sys
from pathlib import Path
import pytest
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port, ready
from finance_core.instance_admission import (
    InstanceAdmissionError, ProductionCloseUncertain, acquire_production_owner,
    admit_production_instance, prepare_production_profile,
)
from finance_core.lifecycle_v2 import LedgerService, RuntimeBinding
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1
import finance_core.instance_admission.sessions as sessions

cfg = synthetic_config(Path(sys.argv[1]), pytest.MonkeyPatch())
prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
owner = acquire_production_owner(cfg)
service = LedgerService(owner, Port(), RuntimeBinding(cfg.instance_id, "gen1", "user", "chat"),
                        clock=lambda: 1792800000)
_, _, review = ready(service)
service.confirm(review.review_id, "action1", "confirm1")
reader = EffectiveLedgerReaderV1(service)

def unknown_close(conn):
    raise sqlite3.OperationalError("synthetic actual-close uncertainty")

sessions._close_sqlite_handle = unknown_close
try:
    reader.collect()
except ProductionCloseUncertain as error:
    assert error.owner is owner
    assert owner._active is not None and owner._uncertain
else:
    raise AssertionError("Collection escaped actual-close uncertainty")
for action in (
    owner.close, reader.collect, lambda: acquire_production_owner(cfg, timeout_seconds=0)
):
    try:
        action()
    except InstanceAdmissionError:
        pass
    else:
        raise AssertionError("Unknown-close owner or lease was released")
# The isolated process owns these intentionally retained handles and leases.
os._exit(0)
"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "tests"), str(root), env.get("PYTHONPATH", "")))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
