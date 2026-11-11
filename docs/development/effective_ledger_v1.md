# Native effective ledger read contract v1

This additive, opt-in API reads verified native personal-expense facts from
`lifecycle_v2`. It supplies one current-event view for queries, export, currency
summaries and read-only reconciliation candidates. It changes no write,
confirmation, correction, migration, recovery or runtime-admission rules.
Existing readers, legacy `transactions` consumers and package root exports keep
their existing behavior. This source contract assigns no release identity.

Import the feature modules explicitly:

```python
from finance_core.lifecycle_v2.effective_v1 import (
    EffectiveFilterV1,
    EffectiveLedgerReaderV1,
    ReadLimitsV1,
)
from finance_core.reconciliation.native_lifecycle_v1 import (
    NativeReconciliationAdapterV1,
)

reader = EffectiveLedgerReaderV1(service)
collection = reader.collect(EffectiveFilterV1(currency="SGD"))
summary = reader.summary(EffectiveFilterV1(currency="SGD"))
candidates = NativeReconciliationAdapterV1(reader).fetch_candidates()
```

`service` is an actual `LedgerService` with its protected owner and trusted
evidence port. No arbitrary connection, cached fact or deserialized DTO supplies
read verification or current write authority. Separate calls are separate read
observations; a later correction can legitimately change their results.

## Current facts, history and original evidence

`get(record_id)` returns `EffectiveFactV1` or `None` for an absent identity.
Retained confirmation/decision evidence with a missing fact is an integrity
failure, not ordinary absence. Each current fact contains:

- `format="finance-effective-fact-v1"`, record ID, economic event ID, version,
  fact SHA-256 and commit sequence;
- `transaction_type="personal_expense"`, canonical string amount and currency,
  transaction date, merchant, description and category;
- `source: SourceIdentityV1`, with source ID, source DTO hash, evidence ID,
  source key, original hash, source timestamp and timezone;
- `account_status="unknown"`, `account_id=None`, `status="LOCAL_POSTED"`.

The original source DTO hash and original-content hash identify different
material. Source identity does not imply a transport such as Telegram. The DTO
does not include raw input, attachment paths or provider responses. Account
assignment and balances are not inferred.

The actual retained native payload must have the supported exact field shape,
`intent="personal_expense_log"` and `transaction_type="personal_expense"`.
Positive money alone never implies this financial type. Unsupported or unknown
financial fields are refused; they are not discarded into an expense view.
Income, transfers and refunds still require their own financial services.

`history(record_id, limits=...)` returns a `FactHistoryV1` with explicit
`current_version` and ascending `versions: tuple[FactVersionV1, ...]`, or `None`
for ordinary absence. Historical versions are not current effective facts.
`source(source_id)` returns the authenticated original `VerifiedSource` through
the existing evidence contract. Neither history nor source reads revive old
confirmation rights or change a restored generation.

These new point/history/source methods also verify the complete native ledger:
`get` and `source` use the default capacity limits, and `history` accepts lowered
limits. They therefore share collection scope/capacity refusal. The existing
`LedgerReader` point methods retain their original behavior.

## Complete collection and capacity

`collect(filters=None, limits=None)` returns an immutable
`EffectiveCollectionV1`, with `format="finance-effective-collection-v1"`,
`scope="native-lifecycle-v2"`, normalized filters, read identity,
`complete=True` and `facts` as an ordered tuple. Empty results are complete and
retain the same envelope. Pending proposals and old fact versions do not become
extra current events.

The reader uses one `lifecycle_read_operation` and explicitly begins one SQLite
read transaction. It checks the retained event/decision/fact inventory in both
directions, authenticates record history and sources, then selects current
values, filters and sorts. A deleted whole record or damaged record outside a
requested filter cannot silently reduce the returned total. It returns only
after rollback of the read transaction and successful protected connection
closure. A close-unknown result propagates; no partial result is released.

The scope is native lifecycle facts. Legacy `transactions` are not joined or
duplicated. Import/archive rows, corresponding events or retained sources using
the reserved `legacy:` prefix refuse the request with
`unsupported_ledger_scope`; they are never silently excluded or reinterpreted
as native expenses. The old readers and legacy adapters remain available under
their own contracts. This interface is not a new import path.

`ReadLimitsV1` defaults to `max_records=10_000` and
`max_fact_versions=100_000`, also exported as `MAX_COMPLETE_RECORDS_V1` and
`MAX_COMPLETE_FACT_VERSIONS_V1`. Callers may lower these limits, but cannot
raise them. Exact positive integers are required; booleans are refused.
Capacity applies before filtering. Exactly the limit is accepted; exceeding it
refuses the whole request, including summaries and exports. A narrow date
filter cannot bypass the complete verification budget. These are supported
capacity boundaries, not measured latency guarantees. No pagination or silent
truncation is provided; `LedgerReader.recent()` remains a bounded list, not a
complete export or candidate source.

`EffectiveFilterV1` supports only `date_from`, `date_to` and `currency`.
Dates must be canonical `YYYY-MM-DD`, endpoints are inclusive, and an inverted
range is refused. Currency uses the existing Money registry and is normalized
in the returned filter. Predicates use current corrected values. Ordering is
transaction date ascending, then record ID ascending. No account filter,
merchant pattern, arbitrary SQL or implicit currency conversion is admitted.

`ReadIdentityV1` records runtime-binding digest, trusted-config-binding digest,
and event-tail sequence/hash from that transaction (empty tail: `0/None`).
It is a local read observation, not a signature, independent checkpoint,
remote protection marker or future write permission. Restoring to a new instance
can change this identity while preserving event/record/version/fact identities.
The reader does not detect rollback of an entire previously valid state prefix.

## Summaries and export

`summary(filters=None, limits=None)` consumes the same complete collection and
returns `CurrencySummaryCollectionV1` with its scope, filters, read identity,
completeness and `items`. Each `CurrencySummaryV1` has `currency`, `event_count`
and canonical string `total_amount`. Items are ordered by currency; there is no
cross-currency grand total. Corrections do not add economic events. Money is
validated by the existing Core contract, and aggregation must not silently
round under a caller-modified Decimal context.

`export_json(filters=None, limits=None)` serializes the successful complete
collection with deterministic sorted keys, explicit nulls, canonical string
amounts and no NaN. It returns one JSON string, never a partial stream or file.
It is a read export, not a recoverable backup, signature or executable import
package. Restore may change the envelope's read identity; the entire export
need not remain byte-identical across generations.

## Native reconciliation candidates

`NativeReconciliationAdapterV1(reader).fetch_candidates(filters=None,
limits=None)` calls the actual reader's complete collection once and returns
`NativeCandidateCollectionV1` retaining scope, normalized filters, read identity,
completeness and candidate wrappers. It does not query legacy tables or write.

Each `NativeReconciliationCandidateV1` retains `fact_ref: FactRefV1`
(record/event/version/fact hash/commit sequence), authenticated source identity,
explicit unknown account and `candidate: InternalCandidate`. The matcher-facing
ID is `lifecycle-v2:<record_id>`; it is never a fabricated legacy row ID.
The evidence locator includes record, version and fact hash. Currency, amount,
merchant and date come from the current fact, type is explicitly EXPENSE, and
transport, attachment path and posted date remain unknown.
Candidate amount conversion preserves the verified value independently of the
caller's Decimal precision, without changing that caller's context.

The existing deterministic matcher may read each wrapper's `candidate`. Keep
the wrapper and its ID association with the result so its exact version/hash
binding is not lost. A stale result is a historical read observation. Future
matching-decision persistence or financial adjustments must revalidate current
facts and permissions under their own contract; no such endpoint is provided.
Candidate visibility does not establish account assignment, bank-statement
coverage, complete reconciliation or permission to modify money.

## Errors and verification

`EffectiveReadError`, a `LifecycleError`, exposes a stable `code`:

- `invalid_read_request` for unsupported arguments;
- `complete_read_limit_exceeded` for verification-capacity refusal;
- `unsupported_ledger_scope` for retained imports/archives;
- `unsupported_fact_shape` for unrepresentable financial facts;
- `fact_evidence_invalid` for inconsistent or missing retained evidence.

Instance admission and production-close-unknown exceptions retain their
existing types and containment semantics. No failure returns a partial
collection, smaller summary or subset of candidates.

Synthetic acceptance uses actual admitted owners, trusted synthetic evidence,
guarded confirmation and fresh correction. It covers corrected current values,
separate currencies, complete capacity edges, missing whole records, damaged
evidence outside filters, deterministic export, unknown accounts and preserved
legacy behavior. Actual wheel/sdist installation exercises the same native
event through correction, retry, restart, protected snapshot, pre-fenced restore
and a fresh authorized correction after restore. This does not prove a machine
lost before fencing, a private application's full backup cut, remote recovery,
external Telegram/AI operation or real-data admission.
