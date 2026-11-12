"""Preserved native safety corpus, explicitly exercised on current 062/v2 sources.

Test IDs remain stable from the original v1 suite. Genuine historical 061/v1
bytes and cross-format rejection live in test_native_recovery_material_v2.py;
current sources cannot generate new material pretending to be historical v1.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from lifecycle_recovery_material_support_v1 import (
    RecordingEvidencePortV1,
    SizedSyntheticAuthenticatorV1,
    SyntheticAuthenticatorV1,
    correct_expense,
    encoded,
    make_service,
    resign,
    resign_provider_context,
    response,
    sha,
    stage_expense,
    wire,
)

from finance_core.instance_admission import InstanceAdmissionError, inspect_instance_admission
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1
from finance_core.lifecycle_v2.recovery_material_v2 import (
    ClosureFrontierV2 as ClosureFrontierV1,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    ClosureLimitsV1,
    RecoveryMaterialError,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    check_native_closure_v2 as check_native_closure_v1,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    collect_native_closure_v2 as collect_native_closure_v1,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    decode_native_closure_v2 as decode_native_closure_v1,
)
from finance_core.profile_layout import profile_root

TABLES = ("sources", "proposals", "reviews", "deliveries", "facts", "decisions", "events")


def frontier(payload):
    decoded = decode_native_closure_v1(payload)
    return ClosureFrontierV1(
        package_sha256=sha(payload),
        event_seq=decoded.last_event_seq,
        event_sha256=decoded.last_event_sha256,
    )


def collect(service, port, authenticator, previous=(), **kwargs):
    return collect_native_closure_v1(
        service,
        archive_source=port,
        authenticator=authenticator,
        previous_packages=previous,
        **kwargs,
    )


@pytest.fixture
def material_live(tmp_path, monkeypatch):
    service, port, cfg, authenticator = make_service(tmp_path, monkeypatch)
    yield service, port, cfg, authenticator
    if service.owner._lease.fd >= 0:
        service.owner.close()
    cfg.close()


@pytest.fixture(scope="module")
def detached_case(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    service, port, cfg, authenticator = make_service(
        tmp_path_factory.mktemp("native-closure"), patch
    )
    try:
        source, _, review, original = stage_expense(service, port, "sgd")
        _, _, _, usd = stage_expense(service, port, "usd", amount="7.25", currency="USD")
        first = collect(service, port, authenticator)
        assert service.confirm(review.review_id, "action-sgd", "confirm-sgd") == original
        _, corrected = correct_expense(service, port, source, original)
        dormant_source, _, _, _ = stage_expense(service, port, "dormant-source", stage="capture")
        proposal_source, dormant_proposal, _, _ = stage_expense(
            service, port, "dormant-proposal", stage="propose"
        )
        second = collect(service, port, authenticator, (first,))
        # Ordinary proposal edit keeps its superseded proposal and independent reply too.
        edit_text = "change amount to SGD 14.00"
        port.register_source("proposal-edit-reply", edit_text)
        expected = service.expected_invocation(
            proposal_source.source_id,
            dormant_proposal.version,
            reply_evidence_id="proposal-edit-reply",
        )
        port.register_provider(
            "obs-proposal-edit",
            expected,
            response(edit_text, "reply", {"amount": "14.00", "currency": "SGD"}),
        )
        edited_proposal = service.propose(
            proposal_source.source_id,
            "obs-proposal-edit",
            dormant_proposal.version,
            "propose-edit",
            reply_evidence_id="proposal-edit-reply",
        )
        assert edited_proposal.version == dormant_proposal.version + 1
        _, _, dormant_review, _ = stage_expense(service, port, "dormant-review", stage="review")
        _, _, dormant_delivery, _ = stage_expense(
            service, port, "dormant-delivery", stage="delivery"
        )
        third = collect(service, port, authenticator, (first, second))
        assert collect(service, port, authenticator, (first, second)) == third
        reader = EffectiveLedgerReaderV1(service)
        effective = reader.collect()
        summary = reader.summary()
        with lifecycle_read_operation(service.owner) as conn:
            expected_rows = {
                key: [dict(row) for row in conn.execute("SELECT * FROM l2_" + key)]
                for key in TABLES
            }
            current_values = tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT record_id,economic_event_id,version,fact_sha256,commit_seq,"
                    "source_id,values_json FROM l2_facts f WHERE version="
                    "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) "
                    "ORDER BY record_id"
                )
            )
            assert (
                conn.execute(
                    "SELECT 1 FROM l2_sources WHERE evidence_id='correction-reply'"
                ).fetchone()
                is None
            )
        assert inspect_instance_admission(cfg)["state"] == "ACTIVE"
        archives = dict(port.archives)
        packages = (first, second, third)
        identity = decode_native_closure_v1(third).identity
        expected_frontier = frontier(third)
        db = (
            profile_root(cfg.locator_root, cfg.profile_id, cfg.layout)
            / "runtime/database/finance.db"
        )
        service.owner.close()
        port.destroy()
        db.unlink()
        cfg.close()
        yield SimpleNamespace(
            packages=packages,
            identity=identity,
            frontier=expected_frontier,
            authenticator=authenticator,
            archives=archives,
            rows=expected_rows,
            current_values=current_values,
            effective=effective,
            summary=summary,
            source=source,
            original=original,
            corrected=corrected,
            usd=usd,
            dormant_source=dormant_source,
            dormant_proposal=dormant_proposal,
            dormant_review=dormant_review,
            dormant_delivery=dormant_delivery,
            db=db,
        )
    finally:
        if service.owner._lease.fd >= 0:
            service.owner.close()
        cfg.close()
        patch.undo()


def checked(case, packages=None, **kwargs):
    return check_native_closure_v1(
        case.packages if packages is None else packages,
        authenticator=kwargs.pop("authenticator", case.authenticator),
        expected_identity=kwargs.pop("expected_identity", case.identity),
        expected_frontier=kwargs.pop("expected_frontier", case.frontier),
        **kwargs,
    )


def refused(action, code=None):
    with pytest.raises(RecoveryMaterialError) as error:
        action()
    if code is not None:
        assert error.value.code == code
    return error.value


def changed_chain(case, change, *, index=-1):
    packages = list(case.packages)
    data = wire(packages[index])
    change(data)
    packages[index] = encoded(data)
    # Pin the changed final bytes so negatives exercise inner validation too.
    return tuple(packages), frontier(packages[-1])


def test_three_segments_reconstruct_without_original_database_or_port(detached_case, monkeypatch):
    case = detached_case
    assert not case.db.exists()
    real_connect = sqlite3.connect
    connections = []

    def isolated_only(database, *args, **kwargs):
        assert database == ":memory:", "Checker attempted to open original/external data"
        conn = real_connect(database, *args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", isolated_only)
    result = checked(case)
    assert result.status == "CONTENT_VERIFIED_OFFLINE"
    assert result.frontier == case.frontier and result.identity == case.identity
    assert result.current_values == case.current_values
    assert result.inventory.fact_version_count == 3
    assert result.inventory.event_count == case.frontier.event_seq
    assert len(result.current_values) == 2
    assert len({row[1] for row in result.current_values}) == 2
    assert sorted((fact.currency, fact.amount, fact.version) for fact in case.effective.facts) == [
        ("SGD", "15.00", 2),
        ("USD", "7.25", 1),
    ]
    assert [
        (item.currency, item.event_count, item.total_amount) for item in case.summary.items
    ] == [("SGD", 1, "15.00"), ("USD", 1, "7.25")]
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    assert not hasattr(result, "connection") and not hasattr(result, "config")


def test_all_seven_tables_and_original_artifact_bytes_are_exact(detached_case):
    case = detached_case
    all_rows = {key: [] for key in TABLES}
    all_evidence = {}
    previous = None
    for payload in case.packages:
        data = wire(payload)
        assert data["previous"] == previous
        for key in TABLES:
            all_rows[key].extend(data["rows"][key])
        for entry in data["evidence"]:
            key = (entry["kind"], entry["evidence_id"])
            assert key not in all_evidence
            assert encoded(entry) == case.archives[key]
            all_evidence[key] = entry
            for artifact in entry["artifacts"]:
                original = base64.b64decode(artifact["bytes_b64"], validate=True)
                assert artifact["size"] == len(original) and artifact["sha256"] == sha(original)
        previous = asdict(frontier(payload))
    for key in TABLES:
        assert sorted(map(canonical, all_rows[key])) == sorted(map(canonical, case.rows[key]))
    assert set(all_evidence) == set(case.archives)
    assert ("source", "correction-reply") in all_evidence
    assert any(row["source_id"] == case.dormant_source.source_id for row in all_rows["sources"])
    assert any(
        row["proposal_id"] == case.dormant_proposal.proposal_id for row in all_rows["proposals"]
    )
    assert any(row["review_id"] == case.dormant_review.review_id for row in all_rows["reviews"])
    assert any(
        row["review_id"] == case.dormant_delivery.review_id for row in all_rows["deliveries"]
    )
    final = wire(case.packages[-1])["inventory"]
    assert final["rows_sha256"] == digest(all_rows)
    assert final["evidence_sha256"] == digest([all_evidence[key] for key in sorted(all_evidence)])


@pytest.mark.parametrize("selection", [(0,), (0, 1), (1, 2), (0, 2), (0, 2, 1), (0, 1, 1, 2)])
def test_missing_reordered_duplicate_or_old_prefix_never_downgrades(detached_case, selection):
    case = detached_case
    refused(lambda: checked(case, tuple(case.packages[i] for i in selection)))


@pytest.mark.parametrize(
    "field", ["scope", "core_identity", "migration_identity", "migration_ledger_digest"]
)
def test_identity_is_independently_pinned(detached_case, field):
    case = detached_case
    identity = replace(
        case.identity, **{field: "wrong" if field != "migration_ledger_digest" else "0" * 64}
    )
    refused(lambda: checked(case, expected_identity=identity))


def test_other_valid_source_binding_is_not_admitted_by_package(detached_case):
    case = detached_case
    identity = replace(
        case.identity, source_binding=replace(case.identity.source_binding, principal_id="other")
    )
    refused(lambda: checked(case, expected_identity=identity), "identity_mismatch")


def test_independent_frontier_rejects_valid_older_tail_and_same_seq_other_hash(detached_case):
    case = detached_case
    refused(lambda: checked(case, case.packages[:1]), "frontier_mismatch")
    refused(
        lambda: checked(case, expected_frontier=replace(case.frontier, event_sha256="0" * 64)),
        "frontier_mismatch",
    )
    refused(
        lambda: checked(case, expected_frontier=replace(case.frontier, package_sha256="0" * 64)),
        "frontier_mismatch",
    )


@pytest.mark.parametrize(
    "table", ["sources", "proposals", "reviews", "deliveries", "facts", "decisions", "events"]
)
def test_omitted_history_or_dormant_owned_row_rejects_even_with_new_outer_anchor(
    detached_case, table
):
    case = detached_case
    # Last segment holds dormant review/delivery; first holds the original fact/decision.
    index = (
        -1
        if table in {"reviews", "deliveries"}
        else (1 if table in {"sources", "proposals"} else 0)
    )
    packages = list(case.packages[: index + 1] if index >= 0 else case.packages)
    data = wire(packages[-1])
    assert data["rows"][table]
    data["rows"][table].pop()
    packages[-1] = encoded(data)
    refused(lambda: checked(case, tuple(packages), expected_frontier=frontier(packages[-1])))


@pytest.mark.parametrize(
    "kind,key",
    [
        ("source", "dormant-source"),
        ("source", "correction-reply"),
        ("provider", "obs-dormant-proposal"),
    ],
)
def test_missing_unused_or_independent_reply_evidence_is_not_synthesized(detached_case, kind, key):
    case = detached_case
    packages = list(case.packages[:2])
    data = wire(packages[-1])
    before = len(data["evidence"])
    data["evidence"] = [e for e in data["evidence"] if (e["kind"], e["evidence_id"]) != (kind, key)]
    assert len(data["evidence"]) == before - 1
    packages[-1] = encoded(data)
    refused(lambda: checked(case, tuple(packages), expected_frontier=frontier(packages[-1])))


@pytest.mark.parametrize(
    "kind,role",
    [
        ("source", "original"),
        ("source", "raw-envelope"),
        ("provider", "request"),
        ("provider", "response"),
        ("provider", "raw-envelope"),
        ("delivery", "display"),
        ("action", "raw-envelope"),
    ],
)
def test_missing_actual_artifact_rejects_after_test_key_resigning(detached_case, kind, role):
    case = detached_case
    data = wire(case.packages[0])
    entry = next(e for e in data["evidence"] if e["kind"] == kind)
    entry["artifacts"] = [a for a in entry["artifacts"] if a["role"] != role]
    resign(entry, case.authenticator)
    payload = encoded(data)
    refused(lambda: checked(case, (payload,), expected_frontier=frontier(payload)))


@pytest.mark.parametrize(
    "damage", ["owner", "key", "actor", "original-bytes", "raw-response", "context"]
)
def test_altered_evidence_cannot_trust_its_repaired_package_hash(detached_case, damage):
    case = detached_case
    data = wire(case.packages[0])
    entry = next(
        e
        for e in data["evidence"]
        if e["kind"] == ("provider" if damage in {"raw-response", "context"} else "source")
    )
    if damage in {"owner", "key"}:
        entry[damage + "_id"] = "foreign-root"
    elif damage == "actor":
        entry["value"]["principal_id"] = "other-person"
    elif damage == "original-bytes":
        artifact = next(a for a in entry["artifacts"] if a["role"] == "original")
        artifact.update(
            bytes_b64=base64.b64encode(b"changed").decode(), size=7, sha256=sha(b"changed")
        )
    elif damage == "raw-response":
        entry["value"]["raw_response"] += " "
    else:
        entry["context"]["projection"]["interpretation_context"]["default_currency"] = "USD"
    payload = encoded(data)
    refused(
        lambda: checked(case, (payload,), expected_frontier=frontier(payload)),
        "evidence_authentication_failed" if damage in {"owner", "key", "actor"} else None,
    )


@pytest.mark.parametrize("kind", ["provider", "delivery", "action"])
def test_authenticated_forged_context_cannot_replace_actual_expected(detached_case, kind):
    case = detached_case
    data = wire(case.packages[0])
    entry = next(e for e in data["evidence"] if e["kind"] == kind)
    if kind == "provider":
        entry["context"]["projection"]["current_values"] = {"amount": "999.00"}
        request = encoded(entry["context"])
        artifact = next(a for a in entry["artifacts"] if a["role"] == "request")
        artifact.update(
            bytes_b64=base64.b64encode(request).decode(), size=len(request), sha256=sha(request)
        )
        entry["value"]["request_sha256"] = sha(request)
    else:
        entry["context"]["review_sha256"] = "0" * 64
    resign(entry, case.authenticator)
    payload = encoded(data)
    refused(lambda: checked(case, (payload,), expected_frontier=frontier(payload)))


@pytest.mark.parametrize(
    "damage",
    [
        "unknown-field",
        "unknown-table",
        "unknown-column",
        "unknown-event",
        "duplicate-row",
        "duplicate-evidence",
        "bool-seq",
        "wrong-previous",
    ],
)
def test_structural_protocol_damage_rejects(detached_case, damage):
    case = detached_case
    data = wire(case.packages[0])
    if damage == "unknown-field":
        data["verified"] = True
    elif damage == "unknown-table":
        data["rows"]["sql"] = []
    elif damage == "unknown-column":
        data["rows"]["sources"][0]["sql"] = "DROP TABLE l2_facts"
    elif damage == "unknown-event":
        data["rows"]["events"][0]["kind"] = "transfer"
    elif damage == "duplicate-row":
        data["rows"]["sources"].append(data["rows"]["sources"][0])
    elif damage == "duplicate-evidence":
        data["evidence"].append(data["evidence"][0])
    elif damage == "bool-seq":
        data["first_event_seq"] = True
    else:
        data["first_event_seq"] = 2
    payload = encoded(data)
    refused(
        lambda: checked(
            case,
            (payload,),
            expected_frontier=ClosureFrontierV1(
                package_sha256=sha(payload),
                event_seq=data["last_event_seq"],
                event_sha256=data["last_event_sha256"],
            ),
        )
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"null",
        b'{"a":1,"a":2}',
        b'{"a":1.0}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-0}',
        b'{"a":1e0}',
        b' {"a":1}',
        b"\xef\xbb\xbf{}",
        b'{"a":"\\ud800"}',
        b"\xff",
    ],
)
def test_decoder_rejects_noncanonical_or_wrong_shape_json(payload):
    refused(lambda: decode_native_closure_v1(payload))


@pytest.mark.parametrize(
    "damage", ["whitespace", "duplicate", "ascii-escape", "nested-json", "base64"]
)
def test_valid_shape_still_requires_exact_canonical_bytes(detached_case, damage):
    data = wire(detached_case.packages[0])
    if damage == "whitespace":
        payload = json.dumps(data, ensure_ascii=False).encode()
    elif damage == "duplicate":
        payload = encoded(data).replace(
            b'{"evidence":', b'{"format":"native-closure-segment-v1","evidence":', 1
        )
    elif damage == "ascii-escape":
        data["evidence"][0]["artifacts"][0]["locator"] = "historical://原件"
        payload = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    elif damage == "nested-json":
        data["rows"]["facts"][0]["values_json"] = '{ "amount": "12.00" }'
        payload = encoded(data)
    else:
        artifact = data["evidence"][0]["artifacts"][0]
        artifact["bytes_b64"] += "\n"
        payload = encoded(data)
    refused(lambda: decode_native_closure_v1(payload))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_events": True},
        {"max_events": 0},
        {"max_events": 10001},
        {"max_packages": 33},
        {"max_package_bytes": 16 * 1024 * 1024 + 1},
        {"max_chain_bytes": 64 * 1024 * 1024 + 1},
        {"max_rows": 20001},
        {"max_evidence_entries": 10001},
        {"max_artifact_bytes": 4 * 1024 * 1024 + 1},
        {"max_artifact_total_bytes": 32 * 1024 * 1024 + 1},
        {"max_text_bytes": 1024 * 1024 + 1},
        {"max_json_depth": 65},
    ],
)
def test_limits_are_exact_and_lowering_only(kwargs):
    refused(lambda: ClosureLimitsV1(**kwargs))


def test_chain_limits_apply_to_total_not_only_each_segment(detached_case):
    case = detached_case
    decoded = [wire(payload) for payload in case.packages]
    totals = {
        "max_chain_bytes": sum(map(len, case.packages)),
        "max_packages": len(case.packages),
        "max_events": case.frontier.event_seq,
        "max_rows": sum(len(rows) for data in decoded for rows in data["rows"].values()),
        "max_evidence_entries": sum(len(data["evidence"]) for data in decoded),
        "max_artifact_total_bytes": sum(
            artifact["size"]
            for data in decoded
            for entry in data["evidence"]
            for artifact in entry["artifacts"]
        ),
    }
    for name, total in totals.items():
        assert (
            checked(case, limits=ClosureLimitsV1(**{name: total})).status
            == "CONTENT_VERIFIED_OFFLINE"
        )
        refused(
            lambda: checked(case, limits=ClosureLimitsV1(**{name: total - 1})),
            "material_limit_exceeded",
        )
    maximum = max(map(len, case.packages))
    assert (
        checked(case, limits=ClosureLimitsV1(max_package_bytes=maximum)).frontier == case.frontier
    )
    refused(
        lambda: checked(case, limits=ClosureLimitsV1(max_package_bytes=maximum - 1)),
        "material_limit_exceeded",
    )


@pytest.mark.parametrize("location", ["row-text", "row-json", "archive-context"])
def test_utf8_text_byte_limit_accepts_exact_cap_and_rejects_one_over(detached_case, location):
    case = detached_case
    data = wire(case.packages[0])
    cap = 8192
    # Multibyte input distinguishes UTF-8 bytes from Python character counts.
    if location == "row-json":
        exact = canonical({"note": "界" * 2727})
    else:
        exact = "界" * 2730 + "ab"
    assert len(exact.encode("utf-8")) == cap and len(exact) < cap

    def put(value):
        if location == "row-text":
            data["rows"]["sources"][0]["raw_input"] = value
        elif location == "row-json":
            data["rows"]["proposals"][0]["values_json"] = value
        else:
            entry = next(entry for entry in data["evidence"] if entry["kind"] == "provider")
            entry["context"]["projection"]["boundary_note"] = value
            resign_provider_context(entry, case.authenticator)

    limits = ClosureLimitsV1(max_text_bytes=cap)
    put(exact)
    # These controlled structural vectors are intentionally not content-audit claims.
    decode_native_closure_v1(encoded(data), limits=limits)
    one_over = canonical({"note": "界" * 2727 + "x"}) if location == "row-json" else exact + "x"
    assert len(one_over.encode("utf-8")) == cap + 1
    put(one_over)
    refused(
        lambda: decode_native_closure_v1(encoded(data), limits=limits),
        "material_limit_exceeded",
    )


@pytest.mark.parametrize("location", ["row-json", "archive-context"])
def test_json_depth_accepts_exact_cap_and_rejects_one_over(detached_case, location):
    case = detached_case
    data = wire(case.packages[0])
    limits = ClosureLimitsV1(max_json_depth=16)

    def put(nesting):
        if location == "row-json":
            # JSON-text columns start their own depth at zero, independently of the wire.
            data["rows"]["proposals"][0]["values_json"] = "[" * nesting + "null" + "]" * nesting
        else:
            entry = next(entry for entry in data["evidence"] if entry["kind"] == "provider")
            value = None
            for _ in range(nesting):
                value = [value]
            entry["context"]["projection"]["boundary_note"] = value
            resign_provider_context(entry, case.authenticator)

    # context.projection.boundary_note starts five levels below the package root.
    exact_nesting = 16 if location == "row-json" else 11
    put(exact_nesting)
    decode_native_closure_v1(encoded(data), limits=limits)
    put(exact_nesting + 1)
    refused(
        lambda: decode_native_closure_v1(encoded(data), limits=limits),
        "material_limit_exceeded",
    )


@pytest.mark.parametrize("authentication_size", [2048, 65536])
def test_authentication_capacity_is_independent_of_artifact_limit(
    material_live, authentication_size
):
    service, _, _, _ = material_live
    auth = SizedSyntheticAuthenticatorV1(authentication_size)
    port = RecordingEvidencePortV1(auth)
    service.port = port
    stage_expense(service, port, "small-artifact", stage="capture")
    limits = ClosureLimitsV1(max_artifact_bytes=1024)
    payload = collect(service, port, auth, limits=limits)
    data = wire(payload)
    assert all(a["size"] <= 1024 for entry in data["evidence"] for a in entry["artifacts"])
    assert all(
        len(base64.b64decode(entry["authentication_b64"], validate=True)) == authentication_size
        for entry in data["evidence"]
    )
    decoded = decode_native_closure_v1(payload, limits=limits)
    assert auth.calls > 0
    calls = auth.calls
    result = check_native_closure_v1(
        (payload,),
        authenticator=auth,
        expected_identity=decoded.identity,
        expected_frontier=frontier(payload),
        limits=limits,
    )
    assert result.status == "CONTENT_VERIFIED_OFFLINE" and auth.calls > calls


def test_authentication_one_byte_above_fixed_cap_rejects_public_paths(material_live):
    service, port, _, auth = material_live
    stage_expense(service, port, "authentication-cap", stage="capture")
    original = collect(service, port, auth)
    decoded = decode_native_closure_v1(original)
    data = wire(original)
    too_large = SizedSyntheticAuthenticatorV1(65537)
    for entry in data["evidence"]:
        resign(entry, too_large)
        assert len(base64.b64decode(entry["authentication_b64"], validate=True)) == 65537
        port.archives[(entry["kind"], entry["evidence_id"])] = encoded(entry)
    payload = encoded(data)
    limits = ClosureLimitsV1(max_artifact_bytes=1024)
    refused(lambda: collect(service, port, too_large, limits=limits), "material_limit_exceeded")
    refused(lambda: decode_native_closure_v1(payload, limits=limits), "material_limit_exceeded")
    refused(
        lambda: check_native_closure_v1(
            (payload,),
            authenticator=too_large,
            expected_identity=decoded.identity,
            expected_frontier=replace(frontier(original), package_sha256=sha(payload)),
            limits=limits,
        ),
        "material_limit_exceeded",
    )


def test_collector_no_new_material_and_failed_export_leave_owner_unchanged(material_live):
    service, port, cfg, auth = material_live
    stage_expense(service, port, "first")
    first = collect(service, port, auth)
    refused(lambda: collect(service, port, auth, (first,)), "no_new_material")
    before = service.reader().full_audit()
    saved = port.archives.pop(("source", "first"))
    refused(lambda: collect(service, port, auth), "missing_evidence")
    port.archives[("source", "first")] = saved
    assert service.reader().full_audit() == before
    assert inspect_instance_admission(cfg)["state"] == "ACTIVE"
    assert collect(service, port, auth) == first


def test_collector_does_not_trust_another_valid_chain_at_same_sequence(
    material_live, detached_case
):
    service, port, _, auth = material_live
    # Same admitted identity, same event count, valid authenticated history,
    # but different normal-operation record IDs: this is not this source prefix.
    stage_expense(service, port, "sgd")
    stage_expense(service, port, "usd", amount="7.25", currency="USD")
    error = refused(lambda: collect(service, port, auth, detached_case.packages[:1]))
    assert error.code == "chain_mismatch"


def test_empty_genesis_and_api_do_not_accept_a_database_as_authority(material_live):
    service, port, cfg, auth = material_live
    payload = collect(service, port, auth)
    decoded = decode_native_closure_v1(payload)
    assert (decoded.first_event_seq, decoded.last_event_seq, decoded.last_event_sha256) == (
        1,
        0,
        None,
    )
    result = check_native_closure_v1(
        (payload,),
        authenticator=auth,
        expected_identity=decoded.identity,
        expected_frontier=frontier(payload),
    )
    assert result.current_values == () and result.inventory.event_count == 0
    refused(
        lambda: collect_native_closure_v1(
            SimpleNamespace(owner=service.owner, binding=service.binding, port=port),
            archive_source=port,
            authenticator=auth,
        )
    )
    assert inspect_instance_admission(cfg)["state"] == "ACTIVE"
    with pytest.raises(LifecycleError):
        replace(service.binding, environment="production")


def test_authenticator_must_be_independent_and_return_none(detached_case):
    case = detached_case
    refused(
        lambda: checked(case, authenticator=SyntheticAuthenticatorV1(b"wrong-root")),
        "evidence_authentication_failed",
    )
    refused(lambda: checked(case, authenticator=None))

    class BooleanAuthenticator:
        def verify(self, **kwargs):
            return True

    refused(
        lambda: checked(case, authenticator=BooleanAuthenticator()),
        "evidence_authentication_failed",
    )


def test_collector_transaction_and_cleanup_complete_before_bytes_escape(material_live, monkeypatch):
    import finance_core.lifecycle_v2._recovery_material_engine as material

    service, port, _, auth = material_live
    stage_expense(service, port, "first")
    operation = material.lifecycle_read_operation
    original_source = port.source
    observations = []

    def source(conn, key):
        assert conn.in_transaction
        observations.append(id(conn))
        return original_source(conn, key)

    @contextmanager
    def cleanup_refused(owner):
        with operation(owner) as conn:
            yield conn
            assert not conn.in_transaction
        raise InstanceAdmissionError("synthetic cleanup refused")

    monkeypatch.setattr(port, "source", source)
    with monkeypatch.context() as patch:
        patch.setattr(material, "lifecycle_read_operation", cleanup_refused)
        with pytest.raises(InstanceAdmissionError, match="cleanup refused"):
            collect(service, port, auth)
    assert observations and len(set(observations)) == 1
    assert service.owner._active is None
    assert collect(service, port, auth)


def test_rehashed_fact_event_and_inventory_cannot_override_authenticated_review(detached_case):
    case = detached_case
    data = wire(case.packages[0])
    fact = data["rows"]["facts"][0]
    values = json.loads(fact["values_json"])
    values["amount"] = "999.00"
    fact["values_json"] = canonical(values)
    fact["fact_sha256"] = digest(
        [
            fact["record_id"],
            fact["version"],
            fact["economic_event_id"],
            fact["source_id"],
            values,
            fact["predecessor_sha256"],
        ]
    )
    previous = None
    for event in data["rows"]["events"]:
        payload = json.loads(event["payload_json"])
        if event["seq"] == fact["commit_seq"]:
            payload["fact_sha256"] = fact["fact_sha256"]
            event["payload_json"] = canonical(payload)
        event["previous_sha256"] = previous
        event["event_sha256"] = digest(
            [
                event["seq"],
                event["operation_id"],
                event["kind"],
                event["entity_id"],
                payload,
                previous,
            ]
        )
        previous = event["event_sha256"]
    data["last_event_sha256"] = previous
    data["inventory"]["event_sha256"] = previous
    data["inventory"]["rows_sha256"] = digest(data["rows"])
    for current in data["inventory"]["current_facts"]:
        if current[0] == fact["record_id"]:
            current[2] = fact["fact_sha256"]
    payload = encoded(data)
    # All unkeyed hashes now agree: only the original financial evidence disagrees.
    refused(lambda: checked(case, (payload,), expected_frontier=frontier(payload)))


@pytest.mark.parametrize("different_bytes", [False, True])
def test_previously_archived_evidence_id_cannot_be_reintroduced(detached_case, different_bytes):
    case = detached_case
    first = wire(case.packages[0])
    second = wire(case.packages[1])
    entry = first["evidence"][0]
    if different_bytes:
        artifact = next(a for a in entry["artifacts"] if a["role"] == "raw-envelope")
        raw = b"another independently signed acquisition under the same evidence id"
        artifact.update(bytes_b64=base64.b64encode(raw).decode(), size=len(raw), sha256=sha(raw))
        resign(entry, case.authenticator)
    second["evidence"].append(entry)
    second["evidence"].sort(key=lambda value: (value["kind"], value["evidence_id"]))
    payload = encoded(second)
    refused(lambda: checked(case, (case.packages[0], payload), expected_frontier=frontier(payload)))


@pytest.mark.parametrize(
    "damage", ["legacy-source", "old-capture", "unknown-kind", "schema", "audit"]
)
def test_collector_rejects_unsupported_schema_scope_and_whole_audit_damage(material_live, damage):
    from test_lifecycle_v2 import _adversarial_database

    service, port, _, auth = material_live
    stage_expense(service, port, "first")
    with _adversarial_database(service) as conn:
        if damage == "legacy-source":
            conn.execute("UPDATE l2_sources SET source_key='legacy:unsupported'")
        elif damage == "old-capture":
            event = conn.execute(
                "SELECT seq,payload_json FROM l2_events WHERE kind='capture'"
            ).fetchone()
            old_payload = canonical(json.loads(event["payload_json"])["source"])
            conn.execute(
                "UPDATE l2_events SET payload_json=? WHERE seq=?", (old_payload, event["seq"])
            )
        elif damage == "unknown-kind":
            conn.execute("UPDATE l2_events SET kind='transfer' WHERE kind='confirm'")
        elif damage == "schema":
            conn.execute("ALTER TABLE l2_sources ADD COLUMN unsupported TEXT")
        else:
            conn.execute("UPDATE l2_events SET event_sha256=? WHERE seq=1", ("0" * 64,))
    with pytest.raises((RecoveryMaterialError, InstanceAdmissionError, LifecycleError)):
        collect(service, port, auth)


def test_checker_closes_its_private_database_on_inventory_failure(detached_case, monkeypatch):
    case = detached_case
    data = wire(case.packages[0])
    data["inventory"]["rows_sha256"] = "0" * 64
    payload = encoded(data)
    real_connect = sqlite3.connect
    connections = []

    def record(database, *args, **kwargs):
        assert database == ":memory:"
        conn = real_connect(database, *args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", record)
    refused(
        lambda: checked(case, (payload,), expected_frontier=frontier(payload)), "inventory_mismatch"
    )
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize(
    "damage", ["missing", "null", "array", "missing-source-text", "null-interpretation-context"]
)
def test_malformed_retained_projection_has_public_error_and_closes_database(
    detached_case, monkeypatch, damage
):
    case = detached_case
    data = wire(case.packages[0])
    proposal = data["rows"]["proposals"][0]
    observation = json.loads(proposal["observation_json"])
    expected = observation["expected"]
    if damage == "missing":
        del expected["projection"]
    elif damage == "null":
        expected["projection"] = None
    elif damage == "array":
        expected["projection"] = []
    elif damage == "missing-source-text":
        del expected["projection"]["source_text"]
    else:
        expected["projection"]["interpretation_context"] = None
    proposal["observation_json"] = canonical(observation)
    payload = encoded(data)
    real_connect = sqlite3.connect
    connections = []

    def record(database, *args, **kwargs):
        assert database == ":memory:"
        conn = real_connect(database, *args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", record)
    refused(
        lambda: checked(case, (payload,), expected_frontier=frontier(payload)), "invalid_material"
    )
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_checker_close_failure_releases_no_success_report(detached_case, monkeypatch):
    case = detached_case
    real_connect = sqlite3.connect
    closed = []

    class FailingClose(sqlite3.Connection):
        def close(self):
            sqlite3.Connection.close(self)
            closed.append(True)
            raise sqlite3.OperationalError("synthetic checker close uncertainty")

    def record(database, *args, **kwargs):
        assert database == ":memory:"
        return real_connect(database, *args, factory=FailingClose, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record)
    with pytest.raises((sqlite3.OperationalError, RecoveryMaterialError)):
        checked(case)
    assert closed


def test_checker_rollback_failure_still_closes_database(detached_case, monkeypatch):
    case = detached_case
    data = wire(case.packages[0])
    data["inventory"]["rows_sha256"] = "0" * 64
    payload = encoded(data)
    real_connect = sqlite3.connect
    closed = []
    rolled_back = []

    class FailingRollback(sqlite3.Connection):
        def rollback(self):
            rolled_back.append(True)
            raise sqlite3.OperationalError("synthetic checker rollback failure")

        def close(self):
            sqlite3.Connection.close(self)
            closed.append(True)

    def record(database, *args, **kwargs):
        assert database == ":memory:"
        return real_connect(database, *args, factory=FailingRollback, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record)
    with pytest.raises((sqlite3.OperationalError, RecoveryMaterialError)):
        checked(case, (payload,), expected_frontier=frontier(payload))
    assert rolled_back and closed


def test_binary_envelope_above_text_limit_is_bounded_by_artifact_limit(material_live):
    service, _, _, auth = material_live
    port = RecordingEvidencePortV1(auth, envelope_padding=b"x" * (1024 * 1024 + 1))
    service.port = port
    stage_expense(service, port, "large-envelope", stage="capture")
    payload = collect(service, port, auth)
    data = wire(payload)
    artifact_size = max(a["size"] for e in data["evidence"] for a in e["artifacts"])
    assert artifact_size > 1024 * 1024
    identity = decode_native_closure_v1(payload).identity
    result = check_native_closure_v1(
        (payload,),
        authenticator=auth,
        expected_identity=identity,
        expected_frontier=frontier(payload),
        limits=ClosureLimitsV1(max_artifact_bytes=artifact_size),
    )
    assert result.status == "CONTENT_VERIFIED_OFFLINE"
    refused(
        lambda: check_native_closure_v1(
            (payload,),
            authenticator=auth,
            expected_identity=identity,
            expected_frontier=frontier(payload),
            limits=ClosureLimitsV1(max_artifact_bytes=artifact_size - 1),
        ),
        "material_limit_exceeded",
    )


@pytest.mark.parametrize("fault", ["close", "rollback"])
def test_actual_source_close_unknown_releases_no_bytes_and_retains_owner(tmp_path, fault):
    script = r"""
import os
import sqlite3
import sys
from pathlib import Path
from lifecycle_recovery_material_support_v1 import make_service, stage_expense
from finance_core.instance_admission import (
    InstanceAdmissionError, ProductionCloseUncertain, acquire_production_owner,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    collect_native_closure_v2 as collect_native_closure_v1,
)
import finance_core.instance_admission.sessions as sessions

class Environment:
    def setenv(self, key, value):
        os.environ[key] = value

service, port, cfg, authenticator = make_service(Path(sys.argv[1]), Environment())
stage_expense(service, port, "first")
owner = service.owner

def fail(*args):
    raise sqlite3.OperationalError("synthetic actual-close uncertainty")

if sys.argv[2] == "close":
    sessions._close_sqlite_handle = fail
else:
    sessions._ProductionConnection.rollback = fail
    sessions._rollback_sqlite_handle = fail

def collect():
    return collect_native_closure_v1(service, archive_source=port, authenticator=authenticator)

try:
    collect()
except ProductionCloseUncertain as error:
    assert error.owner is owner
    assert owner._active is not None and owner._uncertain
else:
    raise AssertionError("Material bytes escaped actual-close uncertainty")
for action in (owner.close, collect, lambda: acquire_production_owner(cfg, timeout_seconds=0)):
    try:
        action()
    except InstanceAdmissionError:
        pass
    else:
        raise AssertionError("Uncertain owner or lease was released")
os._exit(0)
"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "tests"), str(root)))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), fault],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
