"""Read current opening requirements for the actual balance roles of a transfer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..contracts import LifecycleError, canonical
from ..service import LedgerService
from ._impacts_v1 import account_inventory
from .contracts import OpeningRefV1, account_ref, exact, opening_ref, validate_as_of
from .movement_contracts import _later
from .reader import DailyAccountReaderV1, DailyReadIdentityV1, DailyReadLimitsV1
from .transfer_amounts_v1 import TransferAccountImpactV1, TransferAmountsV1, TransferRole
from .transfer_facts_v1 import project_transfer_fact_v1

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


def _requirement(
    funds_time: dict[str, Any], account: dict[str, Any]
) -> tuple[OpeningRefV1 | None, dict[str, Any] | None, OpeningRequirement]:
    opening = account["opening"]
    if opening is None or opening["status"] != "ACTIVE":
        return None, None, "no_active_opening"
    boundary = opening["as_of"]
    requirement: OpeningRequirement = (
        "strictly_later" if _later(funds_time, boundary) else "authenticated_reply_required"
    )
    return opening_ref(account), boundary, requirement


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
        reference, as_of, requirement = _requirement(funds_time, account.values)
        roles.append(
            TransferOpeningRoleV1(
                impact,
                canonical(funds_time),
                reference,
                None if as_of is None else canonical(as_of),
                requirement,
            )
        )
    return TransferOpeningRequirementsV1(snapshot.read_identity, tuple(roles))


def require_active_transfer_openings_v1(conn, value, before_seq=None, *, _rows=None):
    """Check an active candidate against the account versions in the applicable ledger prefix.

    The caller owns the protected transaction and authenticates each selected
    reply. This check determines which sort of proof can apply to each role.
    """
    projection = project_transfer_fact_v1(value)
    if value["status"] != "ACTIVE":
        raise LifecycleError("Only an active transfer can establish new opening coverage")
    accounts = {
        item["values"]["account"]["account_id"]: item["values"]
        for item in account_inventory(conn, before_seq, _rows=_rows)
    }
    for effect in projection.effects:
        coverage = effect["coverage"]
        account = accounts.get(effect["account_id"])
        if account is None or asdict(account_ref(account)) != coverage["binding"]["account_ref"]:
            raise LifecycleError("Transfer role account reference is missing or stale")
        if effect["currency"] != account["account"]["currency"]:
            raise LifecycleError("Transfer role currency differs from its current account")
        opening, _, required_basis = _requirement(effect["funds_time"], account)
        if effect["opening_ref"] != (None if opening is None else asdict(opening)):
            raise LifecycleError("Transfer role opening reference is stale")
        basis = (
            "authenticated_reply"
            if required_basis == "authenticated_reply_required"
            else required_basis
        )
        if coverage["basis"] != basis:
            raise LifecycleError("Transfer role coverage does not match its actual opening time")
