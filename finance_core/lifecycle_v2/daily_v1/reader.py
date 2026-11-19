"""Complete typed account/opening views over the existing protected ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.instance_admission.sessions import (
    _require_runtime_scope,
    lifecycle_read_operation,
)

from ..contracts import LifecycleError, digest
from ..reader import audit_evidence_connection
from ..service import LedgerService
from .accounts import (
    MAX_FACT_VERSIONS,
    MAX_RECORDS,
    _BoundService,
    _limits,
    _record_limit,
    bundle_ref,
)
from .contracts import (
    BUNDLE_FORMATS,
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    account_ref,
    opening_ref,
    strict_json,
    validate_bundle,
)


@dataclass(frozen=True)
class DailyReadLimitsV1:
    max_records: int = MAX_RECORDS
    max_fact_versions: int = MAX_FACT_VERSIONS

    def __post_init__(self):
        if (
            type(self.max_records) is not int
            or not 1 <= self.max_records <= MAX_RECORDS
            or type(self.max_fact_versions) is not int
            or not 1 <= self.max_fact_versions <= MAX_FACT_VERSIONS
        ):
            raise LifecycleError("Exact bounded daily read limits required")


def _read_limits(limits, daily_capacity):
    if limits is None:
        return _record_limit(daily_capacity), MAX_FACT_VERSIONS
    if type(limits) is not DailyReadLimitsV1:
        raise LifecycleError("Exact daily read limits required")
    return limits.max_records, limits.max_fact_versions


@dataclass(frozen=True)
class DailyReadIdentityV1:
    runtime_binding_sha256: str
    source_binding_sha256: str
    event_seq: int
    audit_sha256: str | None
    logical_database_sha256: str


@dataclass(frozen=True)
class DailyAccountViewV1:
    account_id: str
    bundle_event_id: str
    name: str
    account_type: str
    currency: str
    account_ref: AccountDefinitionRefV1
    bundle_ref: BundleFactRefV1
    opening_ref: OpeningRefV1 | None
    opening_status: str
    opening_amount: str | None
    opening_time_unknown: bool
    values_json: str

    @property
    def values(self):
        return strict_json(self.values_json)


@dataclass(frozen=True)
class DailyOpeningVersionV1:
    opening_ref: OpeningRefV1
    owner_fact_ref: BundleFactRefV1
    component_json: str

    @property
    def component(self):
        return strict_json(self.component_json)


@dataclass(frozen=True)
class DailyAccountCollectionV1:
    accounts: tuple[DailyAccountViewV1, ...]
    read_identity: DailyReadIdentityV1
    unassigned_personal_expense_count: int
    completeness_gaps: tuple[str, ...]
    fact_version_count: int
    scope: str = "registered-account-opening-v1"
    complete: bool = True


def _view(fact, values):
    a = values["account"]
    o = values["opening"]
    active = o is not None and o["status"] == "ACTIVE"
    return DailyAccountViewV1(
        a["account_id"],
        fact["economic_event_id"],
        a["name"],
        a["account_type"],
        a["currency"],
        account_ref(values),
        bundle_ref(fact),
        opening_ref(values),
        "ACTIVE" if active else "UNKNOWN",
        o["amount"] if active else None,
        bool(active and o["as_of"]["precision"] == "unknown"),
        fact["values_json"],
    )


class DailyAccountReaderV1:
    def __init__(self, service):
        if not isinstance(service, LedgerService):
            raise LifecycleError("Actual LedgerService required")
        service.owner.assert_valid()
        self._service = service

    def _read(self, limits=None):
        base = self._service
        max_records, max_versions = _read_limits(limits, base.daily_capacity)
        with base._runtime_scope(), lifecycle_read_operation(base.owner) as conn:
            _require_runtime_scope(conn, base.binding, base.runtime_admission)
            service = _BoundService(base)
            conn.execute("BEGIN")
            try:
                _limits(conn, daily_capacity=service.daily_capacity)
                if (
                    conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
                ):
                    raise LifecycleError(
                        "unsupported_ledger_scope: imported accounts are unsupported"
                    )
                rows = conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version").fetchall()
                if len(rows) > max_versions or len({r["record_id"] for r in rows}) > max_records:
                    raise LifecycleError("complete_read_limit_exceeded")
                audit = audit_evidence_connection(
                    conn,
                    service.port,
                    service.binding,
                    service.legacy_verifier,
                    daily_capacity=service.daily_capacity,
                )
                current = {}
                history = {}
                openings = {}
                unknown = set()
                from ..contracts import canonical

                for fact in rows:
                    values = strict_json(fact["values_json"])
                    if values.get("format") in BUNDLE_FORMATS:
                        validate_bundle(values)
                        view = _view(fact, values)
                        current[view.account_id] = view
                        history.setdefault(view.account_id, []).append(view)
                        opening = values["opening"]
                        if opening:
                            key = (
                                view.account_id,
                                opening["opening_event_id"],
                                opening["opening_version"],
                            )
                            item = DailyOpeningVersionV1(
                                opening_ref(values), bundle_ref(fact), canonical(opening)
                            )
                            old = openings.setdefault(key, item)
                            if old.component_json != item.component_json:
                                raise LifecycleError("Opening version has contradictory owners")
                    elif values.get("format") == "finance-daily-movement-v1":
                        unknown.discard(fact["record_id"])
                    else:
                        unknown.add(fact["record_id"])
                identity = DailyReadIdentityV1(
                    digest(asdict(service.binding)),
                    service.owner.config.binding_sha256,
                    audit["event_count"],
                    audit["audit_sha256"],
                    _logical_database_digest(conn),
                )
                collection = DailyAccountCollectionV1(
                    tuple(current[k] for k in sorted(current)),
                    identity,
                    len(unknown),
                    ("unassigned_personal_expense",) if unknown else (),
                    len(rows),
                )
                return collection, history, openings
            finally:
                conn.rollback()

    def collect_accounts(self, *, limits=None):
        return self._read(limits)[0]

    def get_account(self, account_id, *, limits=None):
        return next((a for a in self._read(limits)[0].accounts if a.account_id == account_id), None)

    def account_history(self, account_id, *, limits=None):
        return tuple(self._read(limits)[1].get(account_id, ()))

    def opening_history(self, account_id, opening_event_id, *, limits=None):
        values = self._read(limits)[2]
        return tuple(values[k] for k in sorted(values) if k[:2] == (account_id, opening_event_id))
