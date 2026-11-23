"""Compute a transfer's complete review amounts on the admitted ledger snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.money import canonical_money_str

from ..contracts import LifecycleError, canonical, digest
from ..service import LedgerService
from .accounts import DailyAccountServiceV1, _audit, _fingerprint
from .contracts import instruction_ref, strict_json
from .reader import DailyReadIdentityV1
from .transfer_evidence_v1 import require_transfer_opening_replies_v1
from .transfer_facts_v1 import project_transfer_fact_v1, require_transfer_predecessor_v1
from .transfer_opening_v1 import require_active_transfer_openings_v1


@dataclass(frozen=True)
class TransferAccountChangeV1:
    account_id: str
    currency: str
    recorded_change: str
    balance_change: str | None


@dataclass(frozen=True)
class TransferCreatePreviewV1:
    read_identity: DailyReadIdentityV1
    values_json: str
    account_changes: tuple[TransferAccountChangeV1, ...]
    expense_by_currency: tuple[tuple[str, str], ...]


def _balance_changes(effects):
    totals = {}
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        for effect in effects:
            key = (effect["account_id"], effect["currency"])
            baseline = effect["coverage"]["treatment"] != "no_baseline"
            amount = Decimal(effect["amount"])
            recorded, balance = totals.get(key, (Decimal(0), Decimal(0) if baseline else None))
            if balance is not None and effect["coverage"]["treatment"] == "excluded":
                balance += amount
            totals[key] = (recorded + amount, balance)
        return tuple(
            TransferAccountChangeV1(
                account_id,
                currency,
                canonical_money_str(recorded, currency),
                None if balance is None else canonical_money_str(balance, currency),
            )
            for (account_id, currency), (recorded, balance) in sorted(totals.items())
        )


def read_transfer_create_preview_v1(
    service: LedgerService, value: dict[str, Any]
) -> TransferCreatePreviewV1:
    """Read what a candidate would change before a guarded confirmation is created."""
    require_transfer_predecessor_v1(None, value)
    values_json = canonical(value)
    material = strict_json(values_json)
    boundary = DailyAccountServiceV1(service)
    with boundary._read() as (bound, conn):
        audit = _audit(bound, conn)
        fingerprint = _fingerprint(conn)
        original = material["instruction_ref"]
        source = bound.port.source(conn, original["evidence_id"])
        if instruction_ref(asdict(source), bound.binding) != original:
            raise LifecycleError("Transfer preview differs from its authenticated source")
        require_active_transfer_openings_v1(conn, material)
        require_transfer_opening_replies_v1(bound, conn, material)
        if _fingerprint(conn) != fingerprint:
            raise LifecycleError("Transfer preview evidence changed during the protected read")
        projection = project_transfer_fact_v1(material)
        identity = DailyReadIdentityV1(
            digest(asdict(bound.binding)),
            bound.owner.config.binding_sha256,
            audit["event_count"],
            audit["audit_sha256"],
            _logical_database_digest(conn),
        )
        return TransferCreatePreviewV1(
            identity,
            values_json,
            _balance_changes(projection.effects),
            projection.expenses_by_currency,
        )
