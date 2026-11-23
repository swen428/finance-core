"""Read current opening requirements for the actual balance roles of a transfer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..contracts import LifecycleError, canonical
from ..service import LedgerService
from .contracts import OpeningRefV1, exact, validate_as_of
from .movement_contracts import _later
from .reader import DailyAccountReaderV1, DailyReadIdentityV1, DailyReadLimitsV1
from .transfer_amounts_v1 import TransferAccountImpactV1, TransferAmountsV1, TransferRole

OpeningRequirement = Literal["no_active_opening", "strictly_later", "authenticated_reply_required"]


@dataclass(frozen=True)
class TransferOpeningRoleV1:
    impact: TransferAccountImpactV1
    funds_time_json: str
    opening_ref: OpeningRefV1 | None
    opening_as_of_json: str | None
    requirement: OpeningRequirement


@dataclass(frozen=True)
class TransferOpeningRequirementsV1:
    read_identity: DailyReadIdentityV1
    roles: tuple[TransferOpeningRoleV1, ...]


def read_transfer_opening_requirements_v1(
    service: LedgerService,
    amounts: TransferAmountsV1,
    funds_times: dict[TransferRole, dict[str, Any]],
    *,
    limits: DailyReadLimitsV1 | None = None,
) -> TransferOpeningRequirementsV1:
    """Use one audited account snapshot to identify questions before transfer confirmation."""
    if type(amounts) is not TransferAmountsV1:
        raise LifecycleError("Exact transfer amounts required")
    impacts = amounts.account_impacts()
    exact(funds_times, {impact.role for impact in impacts}, "actual transfer funds times")
    times = {role: validate_as_of(value).copy() for role, value in funds_times.items()}

    snapshot = DailyAccountReaderV1(service).collect_accounts(limits=limits)
    accounts = {account.account_id: account for account in snapshot.accounts}
    roles = []
    for impact in impacts:
        account = accounts.get(impact.account.account_id)
        if account is None or account.account_ref != impact.account:
            raise LifecycleError("Transfer role account reference is missing or stale")
        if impact.currency != account.currency:
            raise LifecycleError("Transfer role currency differs from its current account")
        funds_time = times[impact.role]
        if account.opening_status == "ACTIVE":
            opening = account.values["opening"]
            as_of = opening["as_of"]
            requirement: OpeningRequirement = (
                "strictly_later" if _later(funds_time, as_of) else "authenticated_reply_required"
            )
            roles.append(
                TransferOpeningRoleV1(
                    impact,
                    canonical(funds_time),
                    account.opening_ref,
                    canonical(as_of),
                    requirement,
                )
            )
        else:
            roles.append(
                TransferOpeningRoleV1(
                    impact, canonical(funds_time), None, None, "no_active_opening"
                )
            )
    return TransferOpeningRequirementsV1(snapshot.read_identity, tuple(roles))
