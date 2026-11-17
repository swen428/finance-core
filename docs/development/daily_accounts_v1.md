# Daily account and opening-balance contract v1

The explicit opt-in `finance_core.lifecycle_v2.daily_v1` contract extends the
protected lifecycle with cash, bank and credit-card accounts and opening
balances. Its marker is `finance-daily-v1`. It shares the existing SQLite
database, evidence authentication, runtime admission and guarded transactions.
It introduces no second ledger, SQL migration or root API export. The installed
package, opt-in contract and complete 001–062 migration ledger must be bound
together by a consumer's separate admission procedure.

This contract covers account creation and renaming, setting an opening balance,
and correcting or voiding an opening with a provably empty dependency closure.
Account-associated income/expense, current balances after financial movements,
nonempty opening dependencies, transfers and refunds require their respective
later contracts. An opening is a balance baseline, never income.

## Explicit API

Construct `DailyAccountServiceV1(existing_ledger_service)` and
`DailyAccountReaderV1(existing_ledger_service)` from the actual admitted
`LedgerService`. They share its owner, binding, evidence port and runtime
admission. An external process must compose its own admitted service; these
wrappers do not grant a second writer or weaken the original process checks.

| Entry point | Purpose |
| --- | --- |
| `expected_account_create` / `expected_account_change` | Read and authenticate the exact proposal preconditions |
| `propose_account_create` / `propose_account_change` | Append a complete or incomplete authenticated proposal |
| `prepare_review` | Build one complete version-bound card |
| `record_delivery` / `confirm` | Bind actual delivery and independent human action; atomically finalize |
| `lookup_commit_receipt` | Recover the original exact confirmation outcome without posting |
| `pending` | Return bounded, authenticated pending daily proposals |
| `collect_accounts` / `get_account` | Read complete supported scope, then project account views |
| `account_history` / `opening_history` | Read bundle history or distinct opening component versions |

The daily evidence extension returns `VerifiedDailyProposalV1` from
`daily_proposal(connection, observation_id, expected)`. The expected request is
`ExpectedDailyInvocationV1`; the original lifecycle evidence DTOs are unchanged.
Results use `DailyReviewV1`, `DailyCommitReceiptV1`,
`AccountDefinitionRefV1`, `OpeningRefV1` and `BundleFactRefV1`. Read results use
`DailyAccountCollectionV1`, `DailyAccountViewV1`, `DailyOpeningVersionV1`,
`DailyReadIdentityV1` and bounded `DailyReadLimitsV1`.

## Daily evidence callback permissions

During daily operations, evidence callbacks receive the original actual
ledger connection with an operation-local read-only scope. They may perform
supported reads, including the approved read-only metadata queries. They do
not own the surrounding transaction or connection lifetime. Writes,
transaction control, connection close/context management and configuration
changes are refused before execution. Catching such a refusal inside the
callback does not make the operation eligible to proceed.

Use cursors created within that callback scope, consume their results there
and return only the exact supported evidence DTOs with pure values. Cursors
prepared before the scope cannot be advanced or reset by the callback; scope
cursors are closed or expired on exit and cannot later acquire writer rights.
The expected request, including nested values, must remain unchanged.

Original owner, fence, profile, execute/fetch and resource-close checks still
apply. The Core transaction owner restores its own cleanup permissions before
performing rollback or close. Actual close uncertainty retains the existing
UNKNOWN behavior and cannot produce a successful financial receipt.

This is a daily opt-in boundary. Original capture and old lifecycle APIs keep
their existing contracts; the shared base port is not globally replaced.
Independent immutable snapshot audit has its own actual read-only connection
and evidence checks and acquires no production writer admission.
The host's cooperative code trust boundary is unchanged; the scope constrains
the supported connection and cursor APIs.

## One complete confirmation

An account definition and its optional opening occupy one typed account bundle.
The complete card presents both components, their identities, explicit unknown
or exact opening amount, currency and as-of time. One authenticated human
confirmation appends exactly one lifecycle fact, decision and confirm event in
one protected transaction. A failure rolls back all three. An unconfirmed
proposal does not create a final account.

For example, creating a bank account with SGD 1,200.00 as of a stated time is a
single visible decision. Creating an account whose opening is explicitly
unknown produces no opening event. An omitted amount cannot become zero, and
missing account type, currency or as-of time cannot acquire a guessed value.

Provider output remains a proposal. The deterministic compiler rebuilds the
candidate components from authenticated source and response evidence and the
actual current state. Callers cannot supply authoritative final IDs, versions,
hashes, dependency lists or confirmation rights. Incomplete instructions retain
their source and proposal without becoming final facts.

Existing actor, delivery, exact review hash, expiry, current-owner and runtime
rules continue to apply. Confirmation rereads the target and dependencies
inside the guarded write transaction. A stale card must be replaced with a new
complete card. A reply supplying missing information does not itself authorize
posting.

## Persistent incomplete proposals

An incomplete proposal uses the exact
`finance-daily-incomplete-proposal-v1` shape with `format`, `command_kind`,
`fields`, `reason_span` and `missing_fields`. The partial fields and reason span
come from the authenticated provider command. Core computes the nonempty,
ordered missing-field list and independently rebuilds the stored value during
audit. An empty list must compile into the complete bundle format instead.

Missing fields may remain pending. Invalid supplied values, unknown keys,
wrong types, unsupported currency and invalid reason spans are refused rather
than disguised as missing data. A decimal amount without a known currency
retains its original finite string until the currency's precision can be
checked; it is never rounded using a default currency.

The first create proposal may bind the original captured instruction. Each
subsequent create proposal and every existing-account change binds a genuine
new reply. The provider restates the current complete or partial field set;
Core does not silently merge or overwrite older proposal values. Superseded
drafts and their replies remain in the authenticated proposal history.

`propose_account_change(account_id, observation_id, expected_bundle_ref,
operation_id, *, reply_evidence_id, command_kind)` requires both keyword
arguments. Core builds the exact expected request from the actual source,
reply, target and current proposal before invoking the trusted proposal port.
The authenticated raw command must match that request and command kind.

Incomplete values cannot enter new review construction, historical review
verification or final fact compilation. They remain covered by complete audit,
pending reads and snapshot sealing, including when a later proposal has
superseded them. Completing a draft creates a new proposal and complete card;
it does not reuse the old observation or grant confirmation rights.

## Identities and history

Each account has a stable `account_id` and a bundle record chain. Names may
collide; a name alone cannot select one of several accounts. Account type and
currency are fixed for that identity. Renaming updates only the definition
component, keeping its account ID and the opening reference unchanged.

The bundle's `record_id` is the account ID. Its underlying
`economic_event_id`, exposed as `bundle_event_id` by the daily account view,
identifies the persistent bundle chain. An opening is a distinct financial
component with its own `opening_event_id`; a bundle rename does not manufacture
another opening financial event.

Definition and opening components each have independent versions, predecessor
hashes and content hashes. Every confirmed change also advances the bundle
record version. A component carried unchanged through a later bundle version
retains its identity, version and hash. History projection counts its original
appearance once rather than inventing repeated financial changes.

| Operation | Definition | Opening |
| --- | --- | --- |
| Create | New stable account, definition version 1 | Explicitly unknown or a new ACTIVE opening at version 1 |
| Rename | Same account, next definition version | Carried unchanged |
| Set opening | Carried unchanged | Previously unknown/VOID; a never-used opening ID at version 1 |
| Correct opening | Carried unchanged | Same ACTIVE opening ID, next version |
| Void opening | Carried unchanged | Same opening ID, next version with VOID status |

Voiding an opening retains the account and its complete history; the account's
opening becomes unknown. That opening ID remains permanently closed. A later
explicit setting creates a new opening ID after a new complete confirmation.
Old cards and ordinary edits cannot revive a closed opening.

Exact original-operation retries recover the original persisted receipt, even
after subsequent legitimate changes. They do not add another decision or
financial effect. A different operation cannot borrow the original review or
human action. An uncertain outcome retains its original operation identity and
must be resolved through read-only outcome lookup.

## Amount and time meaning

Opening amounts use exact `Money` values and the currency's declared minor-unit
precision, including two decimal places for MYR. Floating point, excess
precision and implicit rounding are refused. Account and opening currencies
must agree.

All account types use a signed net-asset amount. A credit-card opening of
SGD -300.00 means SGD 300.00 owed; a positive opening means an overpayment.
Cash and bank openings can also represent a real negative amount. Explicit zero
and unknown remain different values.

An opening carries its actual as-of time and interpretation zone. Missing or
uncertain timing cannot silently become the current time or midnight. An
opening correction records the authenticated human reason and before/after
values in the complete review.

## Opening dependencies

Setting, correcting, voiding and setting a new opening after VOID all require
a complete account-based dependency check. It covers all supported current
financial types and any current account impacts, including impacts without an
opening reference. Looking up only records referencing the previous opening ID
would be incomplete.

This version permits these operations only when the actual authenticated
closure is empty. Legacy personal expenses retain unknown account ownership;
they are not arbitrarily assigned to an account. Unknown formats, unreadable
dependencies, nonempty impacts or incomplete domain coverage refuse the
operation. A caller-supplied empty collection cannot prove emptiness.

The review binds the exact target and derived closure. Confirmation recomputes
both under the same protected transaction and rejects changes. Historical audit
reconstructs the confirmed event prefix, so later valid data cannot rewrite the
meaning of an earlier correct decision. Introducing an account-impact writer
requires extending both the inventory and this shared dependency protocol
before its first write.

## Complete reads and audit

The daily account reader returns one audited read identity, stable account and
component references, opening history, explicit unknown state and completeness
information. The supported scope is registered account/opening data together
with original native personal-expense history. Legacy unassigned expenses
remain visible as an account-completeness gap; they are not deducted from an
arbitrary balance. Unsupported nonempty imported/archive domains are refused.

Collection `complete` describes successful complete reading of that declared
scope. It does not declare every account's opening known or provide a complete
current financial balance. Opening status, timing precision and
`completeness_gaps` retain their distinct meanings. Each reader call has its own
read identity; callers needing a consistent collection use its account views
rather than combining separately timed calls.

An account view's `opening_ref` identifies its stored component, including a
VOID component retained for history. A non-null reference alone does not mean
an available baseline: require `opening_status == "ACTIVE"` and its exact
component reference. A voided opening has status `UNKNOWN` and amount `None`
in the view; a never-set opening additionally has no component reference.

Complete audit validates proposals, pending reviews, every fact version,
component transitions, original evidence and the bidirectional event/fact/
decision inventory. It checks ID ownership, continuous versions, predecessors,
permitted changes, permanent opening closure and original confirmation-prefix
dependencies. Both source and independently opened snapshot copies perform the
registered semantic audit. The existing SnapshotCut binds bundle watermarks,
all history and the complete logical database digest.

| Existing entry point | Behavior with daily data |
| --- | --- |
| Old exact personal-expense record lookup | Returns the original old shape for a selected old record |
| Old lookup selecting a daily record | Explicit unsupported-record error |
| Old bounded recent/pending selection | Refuses a selected daily item; does not silently skip it |
| Old complete effective-ledger reads, summaries, export and reconciliation | Refuses the complete request when a daily fact exists, before filtering |
| Registered full audit and snapshot sealing | Audits both registered old and daily formats; unknown formats fail |
| Old prepared/native recovery material | Retains its original personal-expense domain, including refusal of pending-only daily material |

Old DTO fields, canonical bytes, hashes, original eight-currency admission and
legacy migration/preflight bytes keep their original contracts. Adding daily
formats does not implicitly upgrade historical records or recovery protocols.

## Refusal bounds and capacity

This slice limits the complete checked inventory to 128 record identities,
2,048 fact versions and 4,096 proposals. Record and version counts include
legacy personal-expense history. `DailyReadLimitsV1` can lower the read bounds;
it cannot enlarge them or authorize a partial complete result. Exceeding a
bound fails the operation explicitly.

These are refusal thresholds, not a performance guarantee at their maxima.
Account/opening correctness and measured small synthetic workloads do not
qualify a long-lived combined income/expense ledger. Shared-audit scaling and
the later complete financial domain need their own measured volume, history,
pending-state and installation evidence. No audit, callback, resource or
transaction check may be skipped to satisfy a timing target.

Source validation, independently installed wheel/sdist acceptance, hosted
validation, merge approval, immutable release publication, consumer admission
and runtime activation remain separate evidence layers under the
[release contract](release_validation_v1.md). This source API grants no provider
access, recovery promotion or real-ledger activation.
