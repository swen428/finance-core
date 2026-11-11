"""Historical 061 bytes, explicit current 062 format, and version separation."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from lifecycle_recovery_material_support_v1 import (
    SyntheticAuthenticatorV1,
    make_service,
    stage_expense,
)

from finance_core.lifecycle_v2 import _recovery_material_engine as engine
from finance_core.lifecycle_v2.contracts import RuntimeBinding
from finance_core.lifecycle_v2.recovery_material_v1 import (
    ClosureFrontierV1,
    ClosureIdentityV1,
    RecoveryMaterialError,
    check_native_closure_v1,
    collect_native_closure_v1,
    decode_native_closure_v1,
)
from finance_core.lifecycle_v2.recovery_material_v2 import (
    ClosureFrontierV2,
    check_native_closure_v2,
    collect_native_closure_v2,
    decode_native_closure_v2,
)

FIXTURE = Path(__file__).with_name("fixtures") / "native_recovery_material_v1_061.json"
VECTOR_SHA256 = "840c8f7975e229189a5b511bd667a2ba4e571fce07a7fef5977de04290855072"


@pytest.fixture(scope="module")
def historical061():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTOR_SHA256
    value = json.loads(raw)
    packages = tuple(base64.b64decode(item, validate=True) for item in value["packages_b64"])
    assert sum(map(len, packages)) == 74477
    assert [hashlib.sha256(item).hexdigest() for item in packages] == value["package_sha256"]
    identity = dict(value["expected_identity"])
    identity["source_binding"] = RuntimeBinding(**identity["source_binding"])
    return (
        value,
        packages,
        ClosureIdentityV1(**identity),
        ClosureFrontierV1(**value["expected_frontier"]),
    )


def check_historical(case, **kwargs):
    _, packages, identity, frontier = case
    return check_native_closure_v1(
        packages,
        authenticator=kwargs.pop("authenticator", SyntheticAuthenticatorV1()),
        expected_identity=kwargs.pop("expected_identity", identity),
        expected_frontier=frontier,
        **kwargs,
    )


def test_genuine_historical061_vector_rebuilds_on062_installation(historical061):
    value, packages, identity, _ = historical061
    result = check_historical(historical061)
    assert json.loads(json.dumps(asdict(result))) == value["expected_check"]
    assert result.inventory.event_count == 12
    assert result.inventory.fact_version_count == 2
    assert len(result.current_values) == 1
    assert decode_native_closure_v1(packages[-1]).identity == identity
    assert len(engine._trusted_paths(engine._V1_POLICY)) == 61
    assert len(engine.TEMP_DB_MIGRATION_PATHS) == 62


@pytest.mark.parametrize("field", ["migration_identity", "migration_ledger_digest"])
def test_historical061_independently_pinned_digest_is_required(historical061, field):
    with pytest.raises(RecoveryMaterialError, match="identity"):
        check_historical(
            historical061, expected_identity=replace(historical061[2], **{field: "0" * 64})
        )


@pytest.mark.parametrize("damage", ["missing", "bytes"])
def test_historical061_installed_prefix_is_exact(historical061, monkeypatch, tmp_path, damage):
    original = engine.TEMP_DB_MIGRATION_PATHS
    if damage == "missing":
        monkeypatch.setattr(engine, "TEMP_DB_MIGRATION_PATHS", original[:60])
    else:
        replacement = tmp_path / original[60].name
        replacement.write_bytes(original[60].read_bytes() + b"\n-- changed historical bytes\n")
        monkeypatch.setattr(
            engine,
            "TEMP_DB_MIGRATION_PATHS",
            replace(original, paths=original.paths[:60] + (replacement,) + original.paths[61:]),
        )
    with pytest.raises(RecoveryMaterialError) as error:
        check_historical(historical061)
    assert error.value.code == "identity_mismatch"


def test_historical061_exact_schema_does_not_use_current062_compiler(historical061, monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("Historical reconstruction used current schema compiler")

    monkeypatch.setattr(engine, "validate_application_database", refused)
    assert check_historical(historical061).status == "CONTENT_VERIFIED_OFFLINE"


def test_historical061_wrong_independent_root_rejects(historical061):
    with pytest.raises(RecoveryMaterialError) as error:
        check_historical(
            historical061, authenticator=SyntheticAuthenticatorV1(b"another-fixed-root")
        )
    assert error.value.code == "evidence_authentication_failed"


def test_current062_collects_only_explicit_v2_and_rejects_mixed_formats(
    tmp_path, monkeypatch, historical061
):
    service, port, cfg, auth = make_service(tmp_path, monkeypatch)
    try:
        stage_expense(service, port, "explicit062")
        with pytest.raises(RecoveryMaterialError) as error:
            collect_native_closure_v1(service, archive_source=port, authenticator=auth)
        assert error.value.code == "identity_mismatch"
        payload = collect_native_closure_v2(service, archive_source=port, authenticator=auth)
        decoded = decode_native_closure_v2(payload)
        frontier = ClosureFrontierV2(
            hashlib.sha256(payload).hexdigest(), decoded.last_event_seq, decoded.last_event_sha256
        )
        service.owner.close()
        port.destroy()
        assert (
            check_native_closure_v2(
                (payload,),
                authenticator=auth,
                expected_identity=decoded.identity,
                expected_frontier=frontier,
            ).inventory.fact_version_count
            == 1
        )
        for decoder, wrong in (
            (decode_native_closure_v1, payload),
            (decode_native_closure_v2, historical061[1][0]),
        ):
            with pytest.raises(RecoveryMaterialError):
                decoder(wrong)
        with pytest.raises(RecoveryMaterialError):
            check_native_closure_v2(
                historical061[1] + (payload,),
                authenticator=auth,
                expected_identity=decoded.identity,
                expected_frontier=frontier,
            )
        with pytest.raises(RecoveryMaterialError):
            check_native_closure_v1(
                historical061[1] + (payload,),
                authenticator=auth,
                expected_identity=historical061[2],
                expected_frontier=historical061[3],
            )
    finally:
        if service.owner._lease.fd >= 0:
            service.owner.close()
        cfg.close()


def test_prepared_material_helper_rejects_unissued_staging_connection(tmp_path, monkeypatch):
    import sqlite3

    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from finance_core.lifecycle_v2.recovery_material_v2 import _build_prepared_native_closure_v2

    service, port, cfg, auth = make_service(tmp_path, monkeypatch)
    ordinary = sqlite3.connect(":memory:")
    ordinary.execute("BEGIN")
    try:

        def refused():
            with pytest.raises(RecoveryMaterialError) as error:
                _build_prepared_native_closure_v2(
                    service,
                    ordinary,
                    fact=(),
                    decision=(),
                    event=(),
                    archive_source=port,
                    authenticator=auth,
                )
            assert error.value.code == "invalid_material"

        refused()  # Actual owner, but no issued active operation.
        with lifecycle_read_operation(service.owner) as actual:
            actual.execute("BEGIN")
            try:
                refused()  # A different real session cannot lend authority.
            finally:
                actual.rollback()
    finally:
        ordinary.rollback()
        ordinary.close()
        service.owner.close()
        cfg.close()
