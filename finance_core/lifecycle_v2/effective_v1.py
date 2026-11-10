"""Complete, authenticated native personal-expense projections (offline opt-in).

Read identities describe the observed transaction, not external checkpoints.
No legacy facts, account inference, FX conversion or runtime activation is provided.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import localcontext
from typing import Any

from finance_core.instance_admission import ProductionOwner
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.money import Money, canonical_money_str, minor_units, normalize_currency

from .contracts import LifecycleError, RuntimeBinding, VerifiedSource, canonical, digest
from .reader import audit_evidence_connection
from .service import LedgerService

MAX_COMPLETE_RECORDS_V1 = 10_000
MAX_COMPLETE_FACT_VERSIONS_V1 = 100_000
_SCOPE = "native-lifecycle-v2"
_BUSINESS_FIELDS = frozenset(
    {
        "intent",
        "transaction_type",
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "description",
        "category",
    }
)


class EffectiveReadError(LifecycleError):
    """A complete read refused, with a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _invalid(message: str) -> EffectiveReadError:
    return EffectiveReadError("invalid_read_request", message)


def _text(value: Any, *, optional: bool = False) -> bool:
    return (optional and value is None) or (
        type(value) is str
        and 0 < len(value) <= 512
        and value == value.strip()
        and all(ord(char) >= 32 for char in value)
    )


def _iso_date(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _sha(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class EffectiveFilterV1:
    date_from: str | None = None
    date_to: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if any(
            value is not None and not _iso_date(value) for value in (self.date_from, self.date_to)
        ):
            raise _invalid("Date filters require exact ISO calendar dates")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise _invalid("Date filter start follows end")
        if self.currency is not None:
            try:
                object.__setattr__(self, "currency", normalize_currency(self.currency))
            except ValueError as exc:
                raise _invalid("Unsupported currency filter") from exc


@dataclass(frozen=True)
class ReadLimitsV1:
    max_records: int = MAX_COMPLETE_RECORDS_V1
    max_fact_versions: int = MAX_COMPLETE_FACT_VERSIONS_V1

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_records, MAX_COMPLETE_RECORDS_V1),
            (self.max_fact_versions, MAX_COMPLETE_FACT_VERSIONS_V1),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise _invalid("Read limits must be positive integers within the fixed ceilings")


@dataclass(frozen=True)
class SourceIdentityV1:
    source_id: str
    source_sha256: str
    evidence_id: str
    source_key: str
    original_sha256: str
    source_timestamp: int
    timezone: str = "Asia/Singapore"

    def __post_init__(self) -> None:
        if (
            any(not _text(v) for v in (self.source_id, self.evidence_id, self.source_key))
            or not _sha(self.source_sha256)
            or not _sha(self.original_sha256)
            or type(self.source_timestamp) is not int
            or self.source_timestamp <= 0
            or self.timezone != "Asia/Singapore"
        ):
            raise EffectiveReadError("unsupported_fact_shape", "Invalid verified source identity")


def _validate_fact(fact: Any) -> None:
    if (
        any(not _text(v) for v in (fact.record_id, fact.economic_event_id, fact.merchant))
        or type(fact.version) is not int
        or fact.version < 1
        or type(fact.commit_seq) is not int
        or fact.commit_seq < 1
        or not _sha(fact.fact_sha256)
        or fact.transaction_type != "personal_expense"
        or not _iso_date(fact.transaction_date)
        or not _text(fact.description, optional=True)
        or not _text(fact.category, optional=True)
        or type(fact.source) is not SourceIdentityV1
        or fact.account_status != "unknown"
        or fact.account_id is not None
        or fact.status != "LOCAL_POSTED"
    ):
        raise EffectiveReadError(
            "unsupported_fact_shape", "Unsupported native personal-expense shape"
        )
    _validate_money(fact.amount, fact.currency)


def _validate_money(amount: Any, currency: Any) -> None:
    try:
        if not _text(amount) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount) is None:
            raise ValueError("amount")
        with localcontext() as context:
            context.prec = 1024
            money = Money.from_string(amount, currency)
            if (
                money.currency != currency
                or money.amount <= 0
                or canonical_money_str(money.amount, money.currency) != amount
            ):
                raise ValueError("canonical amount")
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise EffectiveReadError(
            "unsupported_fact_shape", "Canonical positive money required"
        ) from exc


@dataclass(frozen=True)
class EffectiveFactV1:
    record_id: str
    economic_event_id: str
    version: int
    fact_sha256: str
    commit_seq: int
    amount: str
    currency: str
    transaction_date: str
    merchant: str
    description: str | None
    category: str | None
    source: SourceIdentityV1
    format: str = field(default="finance-effective-fact-v1", init=False)
    transaction_type: str = field(default="personal_expense", init=False)
    account_status: str = field(default="unknown", init=False)
    account_id: None = field(default=None, init=False)
    status: str = field(default="LOCAL_POSTED", init=False)

    def __post_init__(self) -> None:
        _validate_fact(self)


@dataclass(frozen=True)
class FactVersionV1:
    record_id: str
    economic_event_id: str
    version: int
    fact_sha256: str
    commit_seq: int
    amount: str
    currency: str
    transaction_date: str
    merchant: str
    description: str | None
    category: str | None
    source: SourceIdentityV1
    format: str = field(default="finance-fact-version-v1", init=False)
    transaction_type: str = field(default="personal_expense", init=False)
    account_status: str = field(default="unknown", init=False)
    account_id: None = field(default=None, init=False)
    status: str = field(default="LOCAL_POSTED", init=False)

    def __post_init__(self) -> None:
        _validate_fact(self)


@dataclass(frozen=True)
class ReadIdentityV1:
    """Observed binding and event tip; this is not an external trusted checkpoint."""

    runtime_binding_sha256: str
    config_binding_sha256: str
    event_seq: int
    event_sha256: str | None

    def __post_init__(self) -> None:
        if (
            not _sha(self.runtime_binding_sha256)
            or not _sha(self.config_binding_sha256)
            or type(self.event_seq) is not int
            or self.event_seq < 0
            or (self.event_seq == 0 and self.event_sha256 is not None)
            or (self.event_seq > 0 and not _sha(self.event_sha256))
        ):
            raise EffectiveReadError("fact_evidence_invalid", "Invalid read transaction identity")


@dataclass(frozen=True)
class EffectiveCollectionV1:
    filters: EffectiveFilterV1
    read_identity: ReadIdentityV1
    facts: tuple[EffectiveFactV1, ...]
    format: str = field(default="finance-effective-collection-v1", init=False)
    scope: str = field(default=_SCOPE, init=False)
    complete: bool = field(default=True, init=False)


@dataclass(frozen=True)
class FactHistoryV1:
    record_id: str
    current_version: int
    read_identity: ReadIdentityV1
    versions: tuple[FactVersionV1, ...]
    format: str = field(default="finance-fact-history-v1", init=False)
    scope: str = field(default=_SCOPE, init=False)
    complete: bool = field(default=True, init=False)


@dataclass(frozen=True)
class CurrencySummaryV1:
    currency: str
    event_count: int
    total_amount: str


@dataclass(frozen=True)
class CurrencySummaryCollectionV1:
    filters: EffectiveFilterV1
    read_identity: ReadIdentityV1
    items: tuple[CurrencySummaryV1, ...]
    format: str = field(default="finance-currency-summary-collection-v1", init=False)
    scope: str = field(default=_SCOPE, init=False)
    complete: bool = field(default=True, init=False)


def _business_fields(raw: str) -> dict[str, Any]:
    values = json.loads(raw)
    if (
        type(values) is not dict
        or set(values) != _BUSINESS_FIELDS
        or values["intent"] != "personal_expense_log"
        or values["transaction_type"] != "personal_expense"
        or not _iso_date(values["transaction_date"])
        or not _text(values["merchant"])
        or not _text(values["description"], optional=True)
        or not _text(values["category"], optional=True)
    ):
        raise EffectiveReadError(
            "unsupported_fact_shape", "Unsupported native personal-expense payload"
        )
    _validate_money(values["amount"], values["currency"])
    return {
        key: values[key]
        for key in ("amount", "currency", "transaction_date", "merchant", "description", "category")
    }


def _source_dtos(service, conn, source_id: str) -> tuple[SourceIdentityV1, VerifiedSource]:
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
        raise EffectiveReadError(
            "fact_evidence_invalid", "Verified source DTO differs from retained source"
        )
    return SourceIdentityV1(
        row["source_id"],
        row["source_sha256"],
        row["evidence_id"],
        row["source_key"],
        row["original_sha256"],
        row["created_at"],
        evidence.timezone,
    ), evidence


def _matches(fact: EffectiveFactV1, filters: EffectiveFilterV1) -> bool:
    return (
        (filters.date_from is None or fact.transaction_date >= filters.date_from)
        and (filters.date_to is None or fact.transaction_date <= filters.date_to)
        and (filters.currency is None or fact.currency == filters.currency)
    )


class EffectiveLedgerReaderV1:
    """All-or-error complete native reads under one guarded SQLite transaction."""

    def __init__(self, service: LedgerService):
        if (
            not isinstance(service, LedgerService)
            or not isinstance(service.owner, ProductionOwner)
            or not isinstance(service.binding, RuntimeBinding)
            or service.owner.config.instance_id != service.binding.instance_id
        ):
            raise _invalid("Actual matching LedgerService and production owner required")
        service.owner.assert_valid()
        self.service = service

    def _read(self, filters=None, limits=None, *, history_id=None, source_id=None):
        filters = EffectiveFilterV1() if filters is None else filters
        limits = ReadLimitsV1() if limits is None else limits
        if type(filters) is not EffectiveFilterV1 or type(limits) is not ReadLimitsV1:
            raise _invalid("Exact versioned filter and limits required")
        with lifecycle_read_operation(self.service.owner) as conn:
            conn.execute("BEGIN")
            try:
                # Scope and inventory are global, regardless of requested filters.
                if (
                    conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
                    or conn.execute(
                        "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') LIMIT 1"
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM l2_sources WHERE "
                        "substr(source_key,1,7)='legacy:' COLLATE BINARY LIMIT 1"
                    ).fetchone()
                ):
                    raise EffectiveReadError(
                        "unsupported_ledger_scope", "Imported ledgers are unsupported"
                    )
                counts = conn.execute(
                    "SELECT count(DISTINCT record_id),count(*) FROM l2_facts"
                ).fetchone()
                if counts[0] > limits.max_records or counts[1] > limits.max_fact_versions:
                    raise EffectiveReadError(
                        "complete_read_limit_exceeded", "Complete ledger exceeds read limits"
                    )
                rows = conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version").fetchall()
                values = [_business_fields(row["values_json"]) for row in rows]
                # This includes reverse event/decision inventory, all historical
                # versions, dormant proposals, and authenticated external evidence.
                with localcontext() as context:
                    context.prec = 1024
                    audit = audit_evidence_connection(
                        conn, self.service.port, self.service.binding, self.service.legacy_verifier
                    )
                read_identity = ReadIdentityV1(
                    digest(asdict(self.service.binding)),
                    self.service.owner.config.binding_sha256,
                    audit["event_count"],
                    audit["audit_sha256"],
                )
                latest = {}
                history = []
                sources = {}
                for row, business in zip(rows, values, strict=True):
                    sid = row["source_id"]
                    if sid not in sources:
                        sources[sid] = _source_dtos(self.service, conn, sid)[0]
                    material = {
                        "record_id": row["record_id"],
                        "economic_event_id": row["economic_event_id"],
                        "version": row["version"],
                        "fact_sha256": row["fact_sha256"],
                        "commit_seq": row["commit_seq"],
                        "source": sources[sid],
                        **business,
                    }
                    latest[row["record_id"]] = EffectiveFactV1(**material)
                    if row["record_id"] == history_id:
                        history.append(FactVersionV1(**material))
                facts = tuple(
                    sorted(
                        (fact for fact in latest.values() if _matches(fact, filters)),
                        key=lambda fact: (fact.transaction_date, fact.record_id),
                    )
                )
                collection = EffectiveCollectionV1(filters, read_identity, facts)
                fact_history = (
                    FactHistoryV1(history_id, history[-1].version, read_identity, tuple(history))
                    if history
                    else None
                )
                source = _source_dtos(self.service, conn, source_id)[1] if source_id else None
                result = collection, fact_history, source
            except EffectiveReadError:
                raise
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                raise EffectiveReadError(
                    "fact_evidence_invalid", "Native ledger evidence is invalid"
                ) from exc
            finally:
                conn.rollback()
        # No result escapes if rollback, actual close, or protected cleanup fails.
        return result

    def collect(self, filters=None, limits=None) -> EffectiveCollectionV1:
        return self._read(filters, limits)[0]

    def get(self, record_id: str) -> EffectiveFactV1 | None:
        if not _text(record_id):
            raise _invalid("Exact record identity required")
        return next((fact for fact in self.collect().facts if fact.record_id == record_id), None)

    def history(self, record_id: str, limits=None) -> FactHistoryV1 | None:
        if not _text(record_id):
            raise _invalid("Exact record identity required")
        return self._read(limits=limits, history_id=record_id)[1]

    def source(self, source_id: str) -> VerifiedSource:
        if not _text(source_id):
            raise _invalid("Exact source identity required")
        return self._read(source_id=source_id)[2]

    def summary(self, filters=None, limits=None) -> CurrencySummaryCollectionV1:
        collection = self.collect(filters, limits)
        totals: dict[str, tuple[int, int]] = {}
        for fact in collection.facts:
            count, total = totals.get(fact.currency, (0, 0))
            # Validated canonical strings have exactly the currency's scale.
            units = int(fact.amount.replace(".", ""))
            totals[fact.currency] = count + 1, total + units
        items = []
        for currency, (count, total) in sorted(totals.items()):
            scale = minor_units(currency)
            digits = str(total).zfill(scale + 1)
            amount = digits if scale == 0 else digits[:-scale] + "." + digits[-scale:]
            items.append(CurrencySummaryV1(currency, count, amount))
        return CurrencySummaryCollectionV1(
            collection.filters, collection.read_identity, tuple(items)
        )

    def export_json(self, filters=None, limits=None) -> str:
        return canonical(asdict(self.collect(filters, limits)))


__all__ = [
    "MAX_COMPLETE_FACT_VERSIONS_V1",
    "MAX_COMPLETE_RECORDS_V1",
    "CurrencySummaryCollectionV1",
    "CurrencySummaryV1",
    "EffectiveCollectionV1",
    "EffectiveFactV1",
    "EffectiveFilterV1",
    "EffectiveLedgerReaderV1",
    "EffectiveReadError",
    "FactHistoryV1",
    "FactVersionV1",
    "ReadIdentityV1",
    "ReadLimitsV1",
    "SourceIdentityV1",
]
