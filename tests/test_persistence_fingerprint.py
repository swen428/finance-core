from decimal import Decimal

import pytest

from finance_core.persistence_fingerprint import canonical_fingerprint


def test_canonical_fingerprint_is_full_deterministic_sha256() -> None:
    first = canonical_fingerprint(schema_version="v1", material={"b": 2, "a": [Decimal("1.20")]})
    second = canonical_fingerprint(schema_version="v1", material={"a": [Decimal("1.20")], "b": 2})
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize("material", [{"value": 1.0}, {"nested": [{"value": 1.0}]}])
def test_canonical_fingerprint_rejects_floats_at_every_depth(material: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="float"):
        canonical_fingerprint(schema_version="v1", material=material)


def test_canonical_fingerprint_rejects_unsupported_values_and_non_string_keys() -> None:
    with pytest.raises(TypeError, match="permitted"):
        canonical_fingerprint(schema_version="v1", material={"value": object()})
    with pytest.raises(TypeError, match="keys"):
        canonical_fingerprint(schema_version="v1", material={1: "value"})  # type: ignore[dict-item]


def test_schema_version_and_material_changes_change_digest() -> None:
    baseline = canonical_fingerprint(schema_version="v1", material={"amount": Decimal("1.20")})
    assert baseline != canonical_fingerprint(
        schema_version="v2", material={"amount": Decimal("1.20")}
    )
    assert baseline != canonical_fingerprint(
        schema_version="v1", material={"amount": Decimal("1.21")}
    )
