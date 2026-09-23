"""Pure JSON normalization for receipt calculator output.

Both initial finalization and post-entry correction persist the complete
calculator result with Decimal values represented as strings.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def _decimal_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Object of type {{obj.__class__.__name__}} is not JSON serializable")


def serialize_receipt_calculation_output(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Deep-convert Decimal values without changing the calculator's shape."""
    return json.loads(json.dumps(snapshot, default=_decimal_default, sort_keys=True))


__all__ = ["serialize_receipt_calculation_output"]
