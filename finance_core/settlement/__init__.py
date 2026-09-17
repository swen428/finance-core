"""Settlement Obligation Runtime v1 -- pure deterministic obligation generation.

No persistence. No SQLite access. No file I/O.
"""

from finance_core.settlement.runtime import (
    SettlementObligation,
    SettlementRuntime,
    generate_obligations,
)

__all__ = [
    "SettlementObligation",
    "SettlementRuntime",
    "generate_obligations",
]
