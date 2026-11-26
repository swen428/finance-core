# Daily ordinary income and expense v1

The explicit `finance_core.lifecycle_v2.daily_v1` API adds
`DailyMovementServiceV1` and `DailyLedgerReaderV1` to the existing admitted
`LedgerService`. It does not change historical personal-expense or account
payloads, receipts, or migrations. All inputs below are proposals. A complete
review, authenticated delivery and separate exact confirmation are required
before a fact exists.

`expected_movement_create` / `propose_movement_create` take a captured source
and expected proposal version. Incomplete proposals remain durable pending
items. A supplemental genuine reply creates a new proposal and new complete
card. `expected_movement_change` / `propose_movement_change` bind the current
`BundleFactRefV1` (a fact reference for either domain), fresh reply and
`movement_correct` or `movement_void`. VOID closes ordinary modification.
Repeating an original operation returns its original receipt; changing its
review/action is refused. A second new VOID reports `movement_already_void`.

`DailyMovementServiceV1.pending_for_source(source_id)` reads the latest
undecided proposal for an exact captured source and the current runtime
binding. It returns the same proposal reference and values as `pending()`,
including the missing fields for an incomplete proposal. It returns `None`
when the source has no eligible proposal. The caller retrieves the source
identity from the original saved interaction. A read is a point-in-time
observation. Any later reply, review or confirmation must pass its own
current-version and authority checks.

The trusted port's `movement_proposal` returns an exact
`VerifiedMovementProposalV1` bound to the complete expected invocation digest.
Its JSON command has exactly `format=finance-daily-movement-command-v1`,
`command_kind`, `fields`, and `reason_span`. Creation/correction fields are:

| Field | Meaning |
|---|---|
| `kind` | `income` or `expense` only |
| `business_date` | Exact ISO date for income/consumption statistics |
| `description`, `category` | Explicit bounded text |
| `account_id`, `account_amount` | Exact stable account and positive actual debit/credit amount |
| `original_amount`, `original_currency` | Positive original consumption amount/currency for expense; income uses actual account money |
| `funds_time` | Existing exact `finance-daily-as-of-v1` instant/date/unknown shape, independent of business date |
| `coverage` | Null, or exact `{answer, span}` with included/excluded/unknown and genuine reply codepoint span |

Money accepts exact decimal strings and supported currency precision, never
float, implicit rounding or guessed FX. Expense impacts are negative and
income positive in net-asset terms. JPY 1000 consumption / SGD 9 actual debit
contributes JPY 1000 expense and SGD -9 account change. Credit-card -300 plus
expense 9 produces debt 309; positive balances display overpayment.
Ordinary income and expense support the two movement kinds in the table. Use
`DailyTransferServiceV1` for transfers, credit-card repayments, and the fees
attached to those transfers. Linked refunds require their own contract.

Without an active opening, impact is `no_baseline`, current balance is null,
and recorded net change remains available. With an opening, only a funds time
strictly later than its boundary automatically establishes excluded. Earlier,
equal, same-day ambiguous or unknown time needs genuine exact included/excluded
reply selection. Accepted quoted selections are `included`, `already included`,
`已包含`, `已经包含`, `excluded`, `not included`, `尚未包含`, `未包含`.
Unknown remains pending. A recognized whole bare choice must agree with its
answer enum. A selected supported choice must retain complete word boundaries;
trimming a fixed `not` prefix from a supported choice is refused, including
inside longer text. This bounded check does not infer arbitrary natural-language
answers. Each decision binds account/opening versions, money,
business date, funds time and instruction evidence. Corrections revalidate
these fields using the new actual reply. Changing description/category still
requires a new complete card and independent confirmation.

A correction replaces the current effect for the same economic event,
including atomic A-account to B-account reassignment. It does not append a
second expense. Correction/VOID `reason_span` must quote the authenticated
reply. VOID uses empty fields, preserves the business content and creates a
new closed fact version with zero current impact/statistics. The complete
card includes before/after values, exact reason, current account/opening
references, coverage and per-account balance-effect changes.

`DailyLedgerReaderV1.collect()` audits all registered current and historical
facts and their actual evidence before returning accounts, movements,
balances, per-currency totals and one shared `read_identity`. Detail/history
retain source and version references. Unassigned historical personal expenses
remain counted once in original-currency expense totals, never silently
assigned to an account. A controlled correction can assign their original
event to an account; a controlled VOID can close it. The unassigned gap is
explicit. Unknown formats, imports outside declared scope, read/evidence
failures and existing bounds refuse complete results; bounds are refusal
thresholds, not a lifetime capacity promise. Old complete expense readers
continue to refuse typed daily payloads.

The original account v1 entry points refuse setting, correcting or voiding an
opening with current effective movement dependencies. The explicit controlled
opening v2 protocol described below permits complete set/correction decisions;
VOID still requires an empty closure. Historical empty-closure bytes remain
unchanged. Source support does not establish installation, release, consumer,
Telegram or real-ledger acceptance.

## Authenticated instruction association

`movement_instruction_link(connection, instruction_evidence_id, expected_target)`
returns the exact scalar `VerifiedMovementInstructionLinkV1(evidence_id,
link_json)`. It independently retrieves preserved original user association
facts. It must not manufacture a relation by copying the current expected
request or an AI-selected target. `MovementInstructionTargetV1` binds runtime,
captured source and hash, original source instruction reference, actual current
instruction reference, record/command, prior proposal ID/version/hash and
current target fact reference. `movement_record_id_v1(source_id)` provides the
pure initial identity after capture, before an adapter registers that original
source's association. Core independently rebuilds and compares that same identity;
callers cannot override final identity or authority with the helper.

The canonical link JSON has exactly `format` (the
`finance-daily-movement-instruction-link-v1` marker), `mode`, `target`,
`association_ref` (`evidence_id`, `sha256`) and `question`:

- `initial_instruction` binds the first captured source itself; no invented
  question is permitted.
- `direct_targeted_instruction` binds a genuine target selection or explicit
  controlled command, independently preserved by the adapter. It has no
  question. A bare included/excluded answer is insufficient for this mode.
- `question_reply` binds an actual delivered question and the person's reply.
  It cannot supply a whole-event VOID instruction. VOID requires a separately
  targeted instruction and genuine reason. The question contains `question_id`,
  `question_version`, `delivery_ref`, `purpose`, `target` and `subject`. The
  question target contains the same source, binding, prior proposal and fact
  context, excluding the future reply's instruction reference.
- An `opening_coverage` question for creation or correction retains
  account and opening references, financial kind, original and actual money
  and currencies, business date, and funds time in its subject. Core compares
  the subject against the proposed financial values exactly. An
  answer to a question about 9 cannot authorize a proposal for 90; the latter
  remains pending until a new matching question/reply or explicit instruction.
- An `account_selection` question applies to a new expense whose previous
  draft is missing only the account. Its subject includes the exact draft
  fields and offered account definitions from the delivered question, plus
  the selected account definition from the person's reply. The delivery
  reference continues to bind the question as it was sent. The adapter
  authenticates the selected definition from the preserved reply association
  separately. Core rechecks the
  prior draft, the exact current definition and the selected account's currency.
  The proposal may fill only the account, and the expense currency must match
  that account. If the opening balance still needs a coverage answer, the
  expense remains pending. A separate complete card and confirmation are
  required for posting.

A pending `movement_correct` proposal missing only `opening_coverage` may use
`finance-daily-movement-instruction-link-v2` inside the existing verified
link envelope. This version requires `question_reply` and the exact pending
correction and current fact. It adds `correction` with the original command
fields excluding coverage and the genuine original `reason_ref`. Core
authenticates that earlier correction source and rebuilds the material from
the preceding proposal at both submission and historical replay. The
follow-up command supplies only the coverage choice and its span and has a
null `reason_span`. Its financial fields must equal the pending proposal.
The entire reply must resolve to an accepted coverage choice. A new linked
correction opening answer requires this version. Historical v1 links retain
their original reader behavior.

The complete correction retains the first instruction and its reason. Its
coverage binding uses the later reply and the
`authenticated_correction_opening_reply` basis. Both remain visible in the
confirmed fact and the separately confirmed new card. An unresolved answer
or a changed question subject leaves the original correction pending. The
original link and coverage formats retain their validation and bytes.
The adapter authenticates actual question version, delivery, reply-to or choice,
actor/client, causal order and eligibility at the original instruction event.
It rejects a superseded question at that point. Historical verification checks
that original eligibility, allowing later genuine questions without invalidating
old accepted history. Core independently rereads the link in current expected
construction and original proposal-prefix audit, and compares it to the sealed
value. The port returns the same preserved relation identity and canonical payload
for historical verification. Missing original relation evidence refuses complete
reads and snapshots.
The original issued-connection read-only callback scope and pure scalar copyout
apply to this callback as to other daily evidence callbacks.

For a correction or reversal that starts with an ambiguous target, the
`finance-daily-movement-instruction-link-v3` link uses `target_selection`.
The original instruction continues to supply the command and reason.
`association_ref` identifies the preserved delivered menu. The adapter
authenticates the actual delivery, the ordered list and original cut, and
verifies the person's independently received reply against that menu.
`question` is null. The new `selection` object contains `offered_facts`
(two to eight ordered, distinct record references), `choice` (one-based),
and `reply_ref` (the instruction reference for the actual selection message).
Core rereads that reply, requires a numeric option matching the selected
fact reference and exact version, and retains the original instruction as
the source of the reason. Finalization requires a separate confirmation of
the complete card. A later opening-coverage answer preserves that separation.
Historical audit rechecks the original menu and choice and rejects reuse of
the instruction, menu, or choice as confirmation.

Application integration still owns durable transport/question/provider journal wiring and its
real service validation. This Core link contract and synthetic port do not
establish Telegram reply routing, full application archive coverage or runtime
activation. Old account/native DTOs and historical payload bytes are unchanged.

`DailyLedgerReaderV1.source()` performs complete audit and the final authenticated
source retrieval in one daily runtime/read transaction. The final callback uses
the same original issued connection, read-only scope and exact scalar/DTO copyout;
source identity and hash are revalidated there. It does not delegate this new
accessor to the historical source reader's separate callback boundary.

## Current effects after a controlled opening change

Account bundle v2 may contain an opening-specific dependency map. One resolver
serves current balances, opening dependency inventory and movement review
before/after effects. An exact current movement fact-reference/role match uses
the map treatment; otherwise the movement must have direct coverage bound to
the current active opening (or lawful `no_baseline` when none is active).
A later amount/date/account correction or VOID supersedes the old exact entry;
it never applies by record ID alone. The map's original creation fact remains
its authority through renames. Missing original facts or an invalid carry chain
refuse a complete read.

`DailyLedgerReaderV1.collect_with_effects_v1()` returns
`DailyLedgerWithEffectsV1(collection, effects)` with the same collection
`read_identity`. Each `DailyResolvedEffectV1.values` explicitly identifies the
exact movement, account, role, current opening, `direct`/`map` source, map owner,
effective treatment and balance impact. Historical movement `values` remain
the original stored v1 coverage; they are never silently replaced by current
interpretation. `collect()` uses the same resolver for balances and preserves
original-currency income/expense statistics and recorded net account change.

New movement cards involving a map use the explicit
`finance-daily-movement-review-v2` format with resolved before/after proof and
exact account dependencies. Existing v1 cards retain their original bytes and
prefix formula. For example, voiding an old debit of 9 that the current opening
map includes has balance change zero. Moving that debit to another account
withdraws its resolved old effect and adds only the new account's direct effect.

Within an eligible protected source audit, movement proposal, review and fact
checks can reconstruct their historical account and opening dependencies from
the operation's complete immutable rows. The same strict commit cut applies to
versioned review effects and original map ownership. Each movement source,
instruction, proposal, delivery and action evidence check still uses the real
port and the original request. A native source check can read the source row
and, after the evidence callback, its unique capture event from the same
protected inventory. Independent snapshot-copy audits and writer
checks for the live target and dependencies use SQL. The private optimization
has the same financial limits and complete-scope requirements as the SQL path.

During `prepare_review` on an admitted single-host synthetic writer, a
participating evidence adapter can call
`review_history_scope_v1.authenticate_movement_review_history_namespaces_v1(connection,
port, authenticate)` from its existing read-only evidence callback. Supply the
exact adapter and a function that completes both namespace scans or raises.
`False` delegates to the adapter's ordinary scans. `True` means those scans
succeeded on the same protected historical audit. Each requested item is still
authenticated against its original evidence. A scope mismatch or scan failure
refuses preparation, including a failure caught within the adapter. The scope
expires before the current proposal's separate validation and the review
append. Other operations and adapters use their existing evidence path.

`collect_with_contributions_v1()` supplies the existing account, movement and
original-currency totals with the exact record versions and original amounts
used for income and expense statistics. It returns resolved balance effects
and the same read identity in the same protected snapshot. Unknown-account
legacy expenses contribute to expense totals and retain their completeness
warning. A correction contributes its current version, and a voided movement
has no current contribution. Consumers can use the read identity to decide
whether a follow-up still describes the same snapshot.


## Transfers and repayments

`DailyTransferServiceV1.propose_create` takes the exact captured instruction,
complete actual debit and credit, fee treatment, and opening coverage for
each affected role. Read `prepare_review` for the complete card. Retain its
verified `record_delivery` before `confirm` accepts a separate human action.
Both legs and any fee share the same fact, decision and audit commit.

`propose_change` takes the original capture, exact active fact reference,
current proposal version and the complete replacement or whole reversal. The
evidence port independently retrieves the later instruction and the exact
`VerifiedTransferChangeTargetV1` by its instruction evidence ID. A correction
with changed terms requires newly bound opening evidence. A reversal keeps the
old amounts, links and fees in history and removes their current effects.
`lookup_commit_receipt` reads the original commitment by exact operation,
review and action identity.

`DailyLedgerReaderV1.collect_with_transfers_v2` returns transfers, account
balances, fees counted once, and currency-specific statistics in one audited
snapshot. Principal enters account balances. `transfer_history_v1` returns
the complete version history, including the separately retained correction or
reversal instruction. An opening correction with transfer dependencies uses a
versioned map with one entry per exact fact and balance role. An account rename
carries that map and its original confirmation identity.

## Linked refunds

`DailyRefundServiceV1.propose` takes the captured instruction, the exact
original expense version, the original-currency reduction, the amount
received in the account currency, and the actual receipt date. The evidence
port retrieves the retained selection by instruction ID. When the date
requires opening coverage, the port retrieves the separately delivered
question and its answer by reply ID.

`prepare_review` binds the selected expense, every current refund allocation,
the latest versions of earlier linked refunds, and the account effects.
`record_delivery` retains the exact card. `confirm` verifies a separate action
and rechecks the same dependencies inside the transaction that appends the
fact, decision and audit event. A changed allowance or opening requires a new
card. Use `lookup_commit_receipt` to recover the outcome of the original
operation after a retry.

Corrections and whole reversals use the original refund capture and its exact
active predecessor. Reversals retain the original amounts and release the
allocation when confirmed. Expense corrections preserve the original currency
and enough original amount to cover all effective refunds. Reverse the linked
refunds through their own authorized flow before reversing the original
expense.

`DailyLedgerReaderV1.collect_with_refunds_v3` reads balances, transfers, gross
expenses, original-currency refunds and net expenses from the same audited
snapshot. The receiving account reflects the actual credit. The original
currency totals reflect the refund allocation. An opening adjustment binds
refund inclusion by the exact fact and credit role. `refund_history_v1`
returns the immutable versions, including separately confirmed reversals.

Call `propose(source_id, values, observation_id, operation_id,
expected_proposal_version=0, replaced=None)`. Pass the last two arguments
by keyword. A valid request that exceeds the current original-currency
allowance is retained as `pending_allowance`. Read the original target,
available amount, requested amount, and active refund count with
`proposal_state_v1`. A later proposal must be prepared against the current
allowance before an eligible card can be issued.

The evidence port implements `refund_target_v1(conn, instruction_id)` and
`refund_opening_reply_v1(conn, reply_id)`. It independently reconstructs
`VerifiedRefundTargetV1(evidence_id, target_json)` and
`VerifiedRefundOpeningReplyV1(reply_evidence_id, question_json)` from the
original retained evidence. The target binds the instruction, transition,
original expense, refund predecessor, and reason span. The delivered
question binds its delivery ID, time and exact credit digest. Both callbacks
retain these identities across historical audit and snapshot readback.

A correction can move a refund to a different expense. The card binds the
two complete allowance inventories and keeps the original receiving account,
actual credit, receipt date and current balance effect. After the correction,
the former expense may be reversed if no effective refund still depends on
it. A known conflict keeps the expense proposal pending and exposes its
linked refund references before card delivery.

When the credit needs an opening answer, submit coverage with
`basis="awaiting_reply"`, `treatment="unknown"`, and a null reply. The draft
reports `pending_opening` with its exact receipt date, credit and opening
reference. Reopen that draft and bind the later delivered question to its
`preparation_id` and credit digest. The later answer creates a new proposal
version. A changed expense version requires a new independently retained
selection that identifies the exact pending preparation.

A later proposal keeps each retained selection and opening answer tied to the
preparation it actually answered. Reuse the exact answer when its credit
digest and reply ID remain the same. A superseded or expired undelivered
card requires a fresh proposal before delivery.

## Unified financial query snapshot

`DailyLedgerReaderV1.collect_query_snapshot_v1(*, limits=None)` returns the
complete financial query snapshot after one protected audit, applying the
same read limits as the V3 reader. The result includes the daily collection
with accounts, balances, gross totals and completeness gaps, along with
original-currency refund and net expense totals. The snapshot and collection
share one `read_identity`.

`transactions` contains original personal expenses, daily income and
expenses, transfers and linked refunds. Account bookkeeping remains in the
account collection. Transaction versions are ordered oldest to newest, with
`current` referring to the last version, including a confirmed reversal if
present. The ledger's business fact material and references are intended for
local trusted callers. Source evidence and original inputs use their own
authorized reads.

Each contribution names a current fact by exact reference and provides its
kind, currency, amount and business date. A transfer contributes only its
expense charges. A refund contributes its original-currency allocation on
the received date. Its actual account credit is part of the refund fact and
account balance calculation. A reversal keeps its history and removes its
contributions. Legacy original expenses keep their original statistics and
unassigned account status. Callers handle date and other filters, pagination,
transport authorization and export using the audited material. Existing V1,
V2, and V3 readers retain their scopes.
