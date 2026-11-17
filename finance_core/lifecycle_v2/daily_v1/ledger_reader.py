"""One complete protected snapshot for movements, account balances and statistics."""

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.instance_admission.sessions import (
    _require_runtime_scope,
    lifecycle_read_operation,
)
from finance_core.money import canonical_money_str

from ..contracts import LifecycleError, VerifiedSource, digest
from ._impacts_v1 import effect, require_effect_baseline
from .accounts import _audit, _BoundService, bundle_ref
from .contracts import BUNDLE_FORMAT, BundleFactRefV1, strict_json
from .movement_contracts import MOVEMENT_FORMAT
from .reader import DailyAccountReaderV1, DailyReadIdentityV1, DailyReadLimitsV1, _view


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


class DailyLedgerReaderV1(DailyAccountReaderV1):
    def _ledger(self, limits=None):
        limits = DailyReadLimitsV1() if limits is None else limits
        if type(limits) is not DailyReadLimitsV1:
            raise LifecycleError("Exact daily read limits required")
        base = self._service
        with base._runtime_scope(), lifecycle_read_operation(base.owner) as conn:
            _require_runtime_scope(conn, base.binding, base.runtime_admission)
            service = _BoundService(base)
            conn.execute("BEGIN")
            try:
                audit = _audit(service, conn)
                rows = conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version").fetchall()
                if (
                    len(rows) > limits.max_fact_versions
                    or len({r["record_id"] for r in rows}) > limits.max_records
                ):
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
                accounts, movements, legacy = {}, [], []
                for identity in sorted(current):
                    view = current[identity]
                    values = view.values
                    if values.get("format") == BUNDLE_FORMAT:
                        accounts[identity] = _view(
                            next(r for r in reversed(rows) if r["record_id"] == identity), values
                        )
                    elif values.get("format") == MOVEMENT_FORMAT:
                        movements.append(view)
                    else:
                        legacy.append(view)
                income, expense = {}, {}
                net = {k: Decimal(0) for k in accounts}
                delta = dict(net)
                with localcontext(
                    Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
                ):
                    for view in movements:
                        values = view.values
                        impact = effect(values)
                        if impact:
                            account = accounts.get(impact["account_id"])
                            if account is None:
                                raise LifecycleError("Movement refers to absent account")
                            require_effect_baseline(values, account.values)
                            net[account.account_id] += Decimal(impact["amount"])
                            if values["coverage"]["treatment"] != "included":
                                delta[account.account_id] += Decimal(impact["amount"])
                            totals = expense if values["kind"] == "expense" else income
                            currency = values["original_currency"]
                            totals[currency] = totals.get(currency, Decimal(0)) + Decimal(
                                values["original_amount"]
                            )
                    from ..effective_v1 import _business_fields

                    for view in legacy:
                        old = _business_fields(view.values_json)
                        currency = old["currency"]
                        expense[currency] = expense.get(currency, Decimal(0)) + Decimal(
                            old["amount"]
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
                    )
                return collection, history
            finally:
                conn.rollback()

    def collect(self, *, limits=None):
        return self._ledger(limits)[0]

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
