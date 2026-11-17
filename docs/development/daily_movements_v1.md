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
Transfers, refunds, separately encoded fees and repayments are unsupported
and must use their future typed workflow rather than ordinary income/expense.

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

This slice deliberately refuses setting, correcting or voiding an opening
with any current effective movement dependency. Historical empty closure
bytes remain unchanged. A later exact nonempty-opening treatment contract is
required before that restriction can be removed. Source support does not
establish installation, release, consumer, Telegram or real-ledger acceptance.

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
- `question_reply` binds an actual delivered `opening_coverage` question for
  creation/correction. It cannot supply a whole-event VOID instruction; VOID
  requires a direct targeted instruction and genuine reason. The question contains `question_id`, `question_version`, `delivery_ref`, `purpose`,
  `target` and `subject`. The question target contains the same source,
  binding, prior proposal and fact context, excluding the future reply's
  instruction reference. Subject contains account/opening references, financial
  kind, original and actual money/currencies, business date and funds time.
  Core compares subject against the proposed financial values exactly. An
  answer to a question about 9 cannot authorize a proposal for 90; the latter
  remains pending until a new matching question/reply or explicit instruction.

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

Application integration still owns durable transport/question/provider journal wiring and its
real service validation. This Core link contract and synthetic port do not
establish Telegram reply routing, full application archive coverage or runtime
activation. Old account/native DTOs and historical payload bytes are unchanged.

`DailyLedgerReaderV1.source()` performs complete audit and the final authenticated
source retrieval in one daily runtime/read transaction. The final callback uses
the same original issued connection, read-only scope and exact scalar/DTO copyout;
source identity and hash are revalidated there. It does not delegate this new
accessor to the historical source reader's separate callback boundary.
