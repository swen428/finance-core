"""One complete protected snapshot for movements, account balances and statistics."""

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.instance_admission.sessions import (
    _require_runtime_scope,
    lifecycle_read_operation,
)
from finance_core.money import canonical_money_str

from ..contracts import LifecycleError, VerifiedSource, canonical, digest
from ._impacts_v1 import effect, resolve_effect_v1, resolve_transfer_effect_v1
from .accounts import _audit, _BoundService, bundle_ref
from .contracts import BUNDLE_FORMATS, BundleFactRefV1, strict_json
from .movement_contracts import MOVEMENT_FORMAT
from .reader import DailyAccountReaderV1, DailyReadIdentityV1, _read_limits, _view
from .refund_facts_v1 import REFUND_FORMAT, project_refund_fact_v1
from .transfer_facts_v1 import TRANSFER_FORMAT, project_transfer_fact_v1


@dataclass(frozen=True)
class DailyBalanceV1:
    account_id: str
    currency: str
    opening_known: bool
    recorded_net_change: str
    balance_delta_since_opening: str
    balance_amount: str | None
    credit_card_debt: str | None
    credit_card_overpayment: str | None


@dataclass(frozen=True)
class DailyMovementViewV1:
    record_id: str
    economic_event_id: str
    source_id: str
    fact_ref: BundleFactRefV1
    values_json: str

    @property
    def values(self):
        return strict_json(self.values_json)


@dataclass(frozen=True)
class DailyLedgerCollectionV1:
    accounts: tuple
    movements: tuple
    balances: tuple
    income_by_currency: tuple
    expense_by_currency: tuple
    read_identity: DailyReadIdentityV1
    unassigned_personal_expense_count: int
    pending_count: int
    completeness_gaps: tuple[str, ...]
    fact_version_count: int
    scope: str = "registered-account-opening-income-expense-v1"
    complete: bool = True


@dataclass(frozen=True)
class DailyResolvedEffectV1:
    material_json: str

    @property
    def values(self):
        return strict_json(self.material_json)


@dataclass(frozen=True)
class DailyLedgerWithEffectsV1:
    collection: DailyLedgerCollectionV1
    effects: tuple[DailyResolvedEffectV1, ...]
    format: str = "finance-daily-ledger-with-effects-v1"

    @property
    def read_identity(self):
        return self.collection.read_identity


@dataclass(frozen=True)
class DailyStatisticContributionV1:
    kind: Literal["income", "expense"]
    currency: str
    amount: str
    business_date: str
    fact_ref: BundleFactRefV1


@dataclass(frozen=True)
class DailyLedgerWithContributionsV1:
    collection: DailyLedgerCollectionV1
    contributions: tuple[DailyStatisticContributionV1, ...]
    effects: tuple[DailyResolvedEffectV1, ...]
    format: str = "finance-daily-ledger-with-contributions-v1"

    @property
    def read_identity(self):
        return self.collection.read_identity


@dataclass(frozen=True)
class DailyLedgerWithTransfersV2:
    collection: DailyLedgerCollectionV1
    transfers: tuple[DailyMovementViewV1, ...]
    contributions: tuple[DailyStatisticContributionV1, ...]
    effects: tuple[DailyResolvedEffectV1, ...]
    format: str = "finance-daily-ledger-with-transfers-v2"

    @property
    def read_identity(self):
        return self.collection.read_identity


@dataclass(frozen=True)
class DailyLedgerWithRefundsV3:
    collection: DailyLedgerCollectionV1
    transfers: tuple[DailyMovementViewV1, ...]
    refunds: tuple[DailyMovementViewV1, ...]
    contributions: tuple[DailyStatisticContributionV1, ...]
    effects: tuple[DailyResolvedEffectV1, ...]
    refund_by_currency: tuple[tuple[str, str], ...]
    net_expense_by_currency: tuple[tuple[str, str], ...]
    format: str = "finance-daily-ledger-with-refunds-v3"

    @property
    def read_identity(self):
        return self.collection.read_identity


def _resolve_current_transfer(values, fact_ref, accounts, *, conn=None):
    """Resolve one exact current fact against each relevant account opening."""
    projection = project_transfer_fact_v1(values)
    ref = asdict(fact_ref)
    resolved = []
    for impact in projection.effects:
        account = accounts.get(impact["account_id"])
        if account is None:
            raise LifecycleError("Transfer role refers to an absent or incompatible account")
        resolved.append(resolve_transfer_effect_v1(conn, impact, ref, account.values))
    return projection.expenses_by_currency, resolved


class DailyLedgerReaderV1(DailyAccountReaderV1):
    def _ledger(
        self,
        limits=None,
        *,
        include_contributions=False,
        include_transfers=False,
        include_refunds=False,
    ):
        base = self._service
        max_records, max_versions = _read_limits(limits, base.daily_capacity)
        with base._runtime_scope(), lifecycle_read_operation(base.owner) as conn:
            _require_runtime_scope(conn, base.binding, base.runtime_admission)
            service = _BoundService(base)
            conn.execute("BEGIN")
            try:
                audit = _audit(service, conn)
                rows = conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version").fetchall()
                if len(rows) > max_versions or len({r["record_id"] for r in rows}) > max_records:
                    raise LifecycleError("complete_read_limit_exceeded")
                current, history = {}, {}
                for row in rows:
                    view = DailyMovementViewV1(
                        row["record_id"],
                        row["economic_event_id"],
                        row["source_id"],
                        bundle_ref(row),
                        row["values_json"],
                    )
                    current[row["record_id"]] = view
                    history.setdefault(row["record_id"], []).append(view)
                accounts, movements, transfers, refunds, legacy = {}, [], [], [], []
                for identity in sorted(current):
                    view = current[identity]
                    values = view.values
                    if values.get("format") in BUNDLE_FORMATS:
                        accounts[identity] = _view(
                            next(r for r in reversed(rows) if r["record_id"] == identity), values
                        )
                    elif values.get("format") == MOVEMENT_FORMAT:
                        movements.append(view)
                    elif values.get("format") == TRANSFER_FORMAT:
                        if not include_transfers:
                            raise LifecycleError(
                                "unsupported_record_format: use the transfer ledger"
                            )
                        transfers.append(view)
                    elif values.get("format") == REFUND_FORMAT:
                        if not include_refunds:
                            raise LifecycleError("unsupported_record_format: use the refund ledger")
                        refunds.append(view)
                    else:
                        legacy.append(view)
                income, expense, refunded = {}, {}, {}
                resolved_effects = []
                contributions = []
                net = {k: Decimal(0) for k in accounts}
                delta = dict(net)
                with localcontext(
                    Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
                ):

                    def add_total(kind, currency, amount, business_date, fact_ref):
                        totals = expense if kind == "expense" else income
                        value = Decimal(amount)
                        totals[currency] = totals.get(currency, Decimal(0)) + value
                        if include_contributions:
                            contributions.append(
                                DailyStatisticContributionV1(
                                    kind, currency, amount, business_date, fact_ref
                                )
                            )

                    for view in movements:
                        values = view.values
                        impact = effect(values)
                        if impact:
                            account = accounts.get(impact["account_id"])
                            if account is None:
                                raise LifecycleError("Movement refers to absent account")
                            resolved = resolve_effect_v1(
                                conn, values, asdict(view.fact_ref), account.values
                            )
                            resolved_effects.append(DailyResolvedEffectV1(canonical(resolved)))
                            net[account.account_id] += Decimal(impact["amount"])
                            delta[account.account_id] += Decimal(resolved["balance_impact"])
                            add_total(
                                values["kind"],
                                values["original_currency"],
                                values["original_amount"],
                                values["business_date"],
                                view.fact_ref,
                            )
                    for view in transfers:
                        values = view.values
                        charges, effects = _resolve_current_transfer(
                            values, view.fact_ref, accounts, conn=conn
                        )
                        for impact in effects:
                            key = impact["account_id"]
                            net[key] += Decimal(impact["recorded_change"])
                            delta[key] += Decimal(impact["balance_impact"])
                            resolved_effects.append(DailyResolvedEffectV1(canonical(impact)))
                        business_date = values["business_date"]
                        for currency, amount in charges:
                            add_total("expense", currency, amount, business_date, view.fact_ref)
                    for view in refunds:
                        projection = project_refund_fact_v1(view.values)
                        credit = projection.credit_effect
                        if credit is None:
                            continue
                        account_id = credit["account_id"]
                        account = accounts.get(account_id)
                        if account is None:
                            raise LifecycleError("Refund credit refers to an absent account")
                        resolved = resolve_transfer_effect_v1(
                            conn, credit, asdict(view.fact_ref), account.values
                        )
                        resolved["format"] = "finance-daily-refund-effect-v1"
                        resolved["refund_fact_ref"] = resolved.pop("transfer_fact_ref")
                        net[account_id] += Decimal(resolved["recorded_change"])
                        delta[account_id] += Decimal(resolved["balance_impact"])
                        resolved_effects.append(DailyResolvedEffectV1(canonical(resolved)))
                        currency = projection.terms.original_currency
                        refunded[currency] = refunded.get(currency, Decimal(0)) + Decimal(
                            projection.terms.original_amount
                        )

                    from ..effective_v1 import _business_fields

                    for view in legacy:
                        old = _business_fields(view.values_json)
                        add_total(
                            "expense",
                            old["currency"],
                            old["amount"],
                            old["transaction_date"],
                            view.fact_ref,
                        )
                    balances = []
                    for key, account in accounts.items():
                        balance = (
                            Decimal(account.opening_amount) + delta[key]
                            if account.opening_amount is not None
                            else None
                        )

                        def fmt(v):
                            return canonical_money_str(v, account.currency)

                        card = account.account_type == "credit_card"
                        balances.append(
                            DailyBalanceV1(
                                key,
                                account.currency,
                                balance is not None,
                                fmt(net[key]),
                                fmt(delta[key]),
                                fmt(balance) if balance is not None else None,
                                fmt(max(-balance, Decimal(0)))
                                if card and balance is not None
                                else None,
                                fmt(max(balance, Decimal(0)))
                                if card and balance is not None
                                else None,
                            )
                        )

                    def totals(values):
                        return tuple(
                            (k, canonical_money_str(v, k)) for k, v in sorted(values.items())
                        )

                    pending = conn.execute(
                        "SELECT count(*) FROM l2_proposals p WHERE p.version=(SELECT max(version) "
                        "FROM l2_proposals WHERE source_id=p.source_id) AND NOT EXISTS "
                        "(SELECT 1 FROM l2_reviews r JOIN l2_decisions d "
                        "ON d.review_id=r.review_id "
                        "WHERE r.proposal_id=p.proposal_id)"
                    ).fetchone()[0]
                    identity = DailyReadIdentityV1(
                        digest(asdict(service.binding)),
                        service.owner.config.binding_sha256,
                        audit["event_count"],
                        audit["audit_sha256"],
                        _logical_database_digest(conn),
                    )
                    gaps = (("unassigned_personal_expense",) if legacy else ()) + (
                        ("unknown_opening",)
                        if any(a.opening_amount is None for a in accounts.values())
                        else ()
                    )
                    scope = "registered-account-opening-income-expense-v1"
                    if include_refunds:
                        scope = "registered-account-opening-income-expense-transfer-refund-v3"
                    elif include_transfers:
                        scope = "registered-account-opening-income-expense-transfer-v2"
                    net_expenses = {
                        currency: expense.get(currency, Decimal(0))
                        - refunded.get(currency, Decimal(0))
                        for currency in expense.keys() | refunded.keys()
                    }
                    refund_amounts, net_amounts = totals(refunded), totals(net_expenses)
                    collection = DailyLedgerCollectionV1(
                        tuple(accounts.values()),
                        tuple(movements),
                        tuple(balances),
                        totals(income),
                        totals(expense),
                        identity,
                        len(legacy),
                        pending,
                        gaps,
                        len(rows),
                        scope=scope,
                    )
                return (
                    collection,
                    history,
                    tuple(resolved_effects),
                    tuple(contributions),
                    tuple(transfers),
                    (tuple(refunds), refund_amounts, net_amounts),
                )
            finally:
                conn.rollback()

    def collect(self, *, limits=None):
        return self._ledger(limits)[0]

    def collect_with_effects_v1(self, *, limits=None):
        collection, _, effects, _, _, _ = self._ledger(limits)
        return DailyLedgerWithEffectsV1(collection, effects)

    def collect_with_contributions_v1(self, *, limits=None):
        collection, _, effects, contributions, _, _ = self._ledger(
            limits, include_contributions=True
        )
        return DailyLedgerWithContributionsV1(collection, contributions, effects)

    def collect_with_transfers_v2(self, *, limits=None):
        collection, _, effects, contributions, transfers, _ = self._ledger(
            limits, include_contributions=True, include_transfers=True
        )
        return DailyLedgerWithTransfersV2(collection, transfers, contributions, effects)

    def collect_with_refunds_v3(self, *, limits=None):
        collection, _, effects, steps, transfers, refund_data = self._ledger(
            limits, include_contributions=True, include_transfers=True, include_refunds=True
        )
        refunds, refunded, net = refund_data
        return DailyLedgerWithRefundsV3(
            collection, transfers, refunds, steps, effects, refunded, net
        )

    def refund_history_v1(self, record_id, *, limits=None):
        """Read the complete verified version history of one linked refund."""
        history = self._ledger(limits, include_transfers=True, include_refunds=True)[1]
        rows = history.get(record_id, ())
        if rows and rows[0].values.get("format") != REFUND_FORMAT:
            raise LifecycleError("Expected a refund record")
        return tuple(rows)

    def transfer_history_v1(self, record_id, *, limits=None):
        """Read all verified versions of a transfer from one complete snapshot."""
        history = self._ledger(limits, include_transfers=True)[1].get(record_id, ())
        if history and history[0].values.get("format") != TRANSFER_FORMAT:
            raise LifecycleError("Expected a transfer record")
        return tuple(history)

    def detail(self, record_id, *, limits=None):
        history = self._ledger(limits)[1].get(record_id, ())
        return history[-1] if history else None

    def history(self, record_id, *, limits=None):
        return tuple(self._ledger(limits)[1].get(record_id, ()))

    def source(self, source_id):
        base = self._service
        with base._runtime_scope(), lifecycle_read_operation(base.owner) as conn:
            _require_runtime_scope(conn, base.binding, base.runtime_admission)
            service = _BoundService(base)
            conn.execute("BEGIN")
            try:
                _audit(service, conn)
                row = service._source(conn, source_id, allow_archived=True)
                evidence = VerifiedSource(
                    row["evidence_id"],
                    row["principal_id"],
                    row["client_id"],
                    row["source_key"],
                    row["raw_input"],
                    row["original_sha256"],
                    row["created_at"],
                )
                if digest(asdict(evidence)) != row["source_sha256"]:
                    raise LifecycleError(
                        "Authenticated daily source DTO differs from retained source"
                    )
                return evidence
            finally:
                conn.rollback()
