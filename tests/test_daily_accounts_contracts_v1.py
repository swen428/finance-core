"""Synthetic exact daily account component and transition contracts."""

import hashlib
import importlib
from dataclasses import asdict, replace
from decimal import Inexact, Rounded, localcontext

import pytest

from finance_core.lifecycle_v2 import RuntimeBinding
from finance_core.lifecycle_v2.contracts import LifecycleError, digest


def api():
    return importlib.import_module("finance_core.lifecycle_v2.daily_v1.contracts")


def expected(
    kind="account_create", before=None, text="Create daily account; reason: wrong opening"
):
    c = api()
    instruction = {
        "evidence_id": "instruction-1",
        "principal_id": "user",
        "client_id": "chat",
        "source_key": "message-1",
        "raw_input": text,
        "original_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_timestamp": 1794877200,
        "timezone": "Asia/Singapore",
    }
    return c.ExpectedDailyInvocationV1(
        RuntimeBinding("instance", "generation", "user", "chat"),
        "source-1",
        "a" * 64,
        0 if before is None else 1,
        "account-1",
        kind,
        before,
        None if before is None else asdict(c.BundleFactRefV1("account-1", 1, "b" * 64, 4)),
        instruction,
        "opening-1",
        None,
        None,
    )


def command(kind="account_create", fields=None, reason_span=None):
    return {
        "format": "finance-daily-command-v1",
        "command_kind": kind,
        "fields": fields
        if fields is not None
        else {"name": "Daily", "account_type": "bank", "currency": "MYR", "opening": None},
        "reason_span": reason_span,
    }


def as_of(precision="unknown", timestamp=None, local_date=None):
    return {
        "format": "finance-daily-as-of-v1",
        "precision": precision,
        "timestamp": timestamp,
        "local_date": local_date,
        "timezone": "Asia/Singapore",
    }


def test_unknown_opening_and_explicit_signed_zero_are_distinct():
    c = api()
    e = expected()
    unknown = c.compile_bundle(command(), e)
    assert unknown["opening"] is None
    for raw, canonical in (("-300", "-300.00"), ("0", "0.00"), ("12.3", "12.30")):
        fields = {
            "name": "Card",
            "account_type": "credit_card",
            "currency": "MYR",
            "opening": {"amount": raw, "as_of": as_of()},
        }
        value = c.compile_bundle(command(fields=fields), e)
        assert value["opening"]["amount"] == canonical
        assert value["opening"]["as_of"] == as_of()
        assert c.validate_bundle(value) == value
        assert c.opening_ref(value).opening_version == 1


@pytest.mark.parametrize("raw", [1.2, True, "NaN", "Infinity", "1.001", "1e2"])
def test_unsafe_money_never_enters_bundle(raw):
    with pytest.raises((LifecycleError, ValueError)):
        api().compile_bundle(
            command(
                fields={
                    "name": "Daily",
                    "account_type": "bank",
                    "currency": "MYR",
                    "opening": {"amount": raw, "as_of": as_of()},
                }
            ),
            expected(),
        )


def test_canonical_money_and_hash_ignore_hostile_decimal_context():
    c = api()
    e = expected()
    cmd = command(
        fields={
            "name": "Daily",
            "account_type": "bank",
            "currency": "MYR",
            "opening": {"amount": "123456789012345678.12", "as_of": as_of()},
        }
    )
    normal = c.compile_bundle(cmd, e)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert c.compile_bundle(cmd, e) == normal
        assert digest(c.compile_bundle(cmd, e)) == digest(normal)


@pytest.mark.parametrize("value", [as_of("date", local_date="2026-11-17"), as_of()])
def test_explicit_time_precision_roundtrips(value):
    assert api().validate_as_of(value) == value


@pytest.mark.parametrize(
    "value",
    [
        as_of("date", timestamp=1, local_date="2026-11-17"),
        as_of("unknown", local_date="2026-11-17"),
        as_of("instant", timestamp=True, local_date="2026-11-17"),
        as_of("instant", timestamp=1794877200, local_date="2000-01-01"),
    ],
)
def test_invalid_time_shape_is_rejected(value):
    with pytest.raises(LifecycleError):
        api().validate_as_of(value)


def test_rename_carries_opening_ref_and_void_requires_fresh_opening_id():
    c = api()
    create = command(
        fields={
            "name": "Daily",
            "account_type": "bank",
            "currency": "MYR",
            "opening": {"amount": "12.30", "as_of": as_of()},
        }
    )
    first = c.compile_bundle(create, expected())
    ref = c.opening_ref(first)
    renamed = c.compile_bundle(
        command("account_rename", {"name": "New name"}), expected("account_rename", first)
    )
    assert renamed["account"]["definition_version"] == 2
    assert c.opening_ref(renamed) == ref
    assert renamed["opening"] == first["opening"]
    text = "Void opening; reason: wrong opening"
    span = [text.index("wrong"), len(text)]
    void = c.compile_bundle(
        command("opening_void", {}, span), expected("opening_void", renamed, text)
    )
    assert void["opening"]["status"] == "VOID"
    assert void["account"] == renamed["account"]
    with pytest.raises(LifecycleError):
        c.compile_bundle(
            command("opening_correct", {"amount": "1", "as_of": as_of()}, span),
            expected("opening_correct", void, text),
        )
    with pytest.raises(LifecycleError):
        c.compile_bundle(
            command("opening_set", {"amount": "1", "as_of": as_of()}), expected("opening_set", void)
        )
    fresh = c.compile_bundle(
        command("opening_set", {"amount": "1", "as_of": as_of()}),
        replace(expected("opening_set", void), opening_event_id="opening-2"),
    )
    assert fresh["opening"]["opening_event_id"] == "opening-2"
    assert fresh["opening"]["opening_version"] == 1


def test_extra_unknown_duplicate_and_missing_fields_are_never_coerced():
    c = api()
    with pytest.raises(LifecycleError):
        c.decode_command('{"format":"a","format":"b"}')
    with pytest.raises(LifecycleError):
        c.compile_bundle({**command(), "extra": True}, expected())
    with pytest.raises(LifecycleError):
        c.compile_bundle(command(fields={"name": "Daily"}), expected())


@pytest.mark.parametrize("missing", ["name", "account_type", "currency", "opening"])
def test_partial_create_keeps_fields_and_core_missing_list(missing):
    c = api()
    raw = command()
    raw["fields"].pop(missing)
    value = c.compile_proposal(raw, expected())
    assert value == {
        "format": "finance-daily-incomplete-proposal-v1",
        "command_kind": "account_create",
        "fields": raw["fields"],
        "reason_span": None,
        "missing_fields": [missing],
    }
    with pytest.raises(LifecycleError):
        c.validate_bundle(value)


def test_partial_opening_and_reason_never_supply_defaults():
    c = api()
    raw = command(fields={"name": "Daily", "account_type": "bank", "opening": {"amount": "1.001"}})
    draft = c.compile_proposal(raw, expected())
    assert draft["missing_fields"] == ["currency", "opening.as_of"]
    assert draft["fields"]["opening"]["amount"] == "1.001"
    raw["fields"]["currency"] = "MYR"
    with pytest.raises(ValueError):
        c.compile_proposal(raw, expected())
    first = c.compile_bundle(
        command(
            fields={
                "name": "Daily",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "1", "as_of": as_of()},
            }
        ),
        expected(),
    )
    void = c.compile_proposal(command("opening_void", {}), expected("opening_void", first))
    assert void["missing_fields"] == ["reason_span"]


def test_supplied_bad_fields_cannot_hide_as_incomplete():
    c = api()
    for fields in (
        {"name": " "},
        {"currency": "XYZ"},
        {"account_type": "other"},
        {"opening": {"amount": "NaN"}},
        {"opening": {"extra": 1}},
        {"extra": 1},
    ):
        with pytest.raises(ValueError):
            c.compile_proposal(command(fields=fields), expected())


def test_daily_money_uses_its_own_exponent_and_rounding_limits():
    c = api()
    e = expected()
    cmd = command(
        fields={
            "name": "Daily",
            "account_type": "bank",
            "currency": "MYR",
            "opening": {"amount": "123456.12", "as_of": as_of()},
        }
    )
    baseline = c.compile_bundle(cmd, e)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        assert c.compile_bundle(cmd, e) == baseline


@pytest.mark.parametrize("kind", [[], {}, True, None, 1])
def test_wrong_discriminator_types_raise_the_daily_contract_error(kind):
    from finance_core.lifecycle_v2.contracts import canonical

    with pytest.raises(LifecycleError):
        api().decode_command(canonical({**command(), "command_kind": kind}))


def test_component_self_hash_does_not_legitimize_invalid_reason_shape():
    c = api()
    text = "Void; reason: incorrect"
    span = [text.index("incorrect"), len(text)]
    first = c.compile_bundle(
        command(
            fields={
                "name": "Daily",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "1", "as_of": as_of()},
            }
        ),
        expected(),
    )
    value = c.compile_bundle(
        command("opening_void", {}, span), expected("opening_void", first, text)
    )
    value["opening"]["reason_ref"]["span"] = [-1, 4]
    value["opening"]["opening_sha256"] = digest(
        {k: v for k, v in value["opening"].items() if k != "opening_sha256"}
    )
    with pytest.raises(LifecycleError):
        c.validate_bundle(value)


def test_valid_instant_and_negative_zero_have_exact_explicit_meaning():
    timestamp = 1794877200
    day = "2026-11-17"  # 09:00 Singapore, explicitly supplied by the user.
    value = as_of("instant", timestamp, day)
    c = api()
    assert c.validate_as_of(value) == value
    bundle = c.compile_bundle(
        command(
            fields={
                "name": "Card",
                "account_type": "credit_card",
                "currency": "MYR",
                "opening": {"amount": "-0.00", "as_of": value},
            }
        ),
        expected(),
    )
    assert bundle["opening"]["amount"] == "0.00"
    assert bundle["opening"]["as_of"]["timestamp"] == timestamp
