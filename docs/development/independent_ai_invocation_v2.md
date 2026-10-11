# Independent AI expense interpretation and full-card confirmation v2

This contract covers text and total-level personal receipts, natural changes,
clarification and one guarded economic event. The model proposes; Python
validates and calculates; an authenticated person approves the complete actually
delivered card. Network/transport adapters and runtime activation are separate.
PDF parsing and refund posting are not included.

## Versions and fixed composition

New independent input/result/publication/interpretation-input use v2; full-card
Posting review/human-decision use v2. Independent v1 is historically readable,
not a new invocation route. Delivered legacy AI and Posting v1 retain their own
proof domains and six-HUMAN qualification. Explicit persisted schema selects a
reader; field presence or failed v2 proof cannot downgrade its domain.

Connection, trusted source/binding, input verifier, provider observation authority,
policy, timezone, default currency and clock are fixed composition. Operations
cannot select actors, verifiers, endpoints, models or keys. Old compatibility
receipts cannot authenticate new provider observations. The packaged feature
resource binds new interfaces/subschemas/minimum059 ledger; root API version or
package version alone does not identify this capability.

## Interpretation and deterministic calculation

Input binds original authorized source, exact authenticated reply, actual
interaction ID/hash, current six fields, verified partial draft/questions and
trusted source/reply time context. Cached drafts never create revision authority.
Only necessary bounded text is sent; zero original/derived images, URLs, base64,
unrelated conversation or credential data. No OCR-failure cloud-vision fallback.

Result exact keys: schema, outcome, intent, operations, coverage, unresolved.
Outcome is proposal/clarification/unsupported; only personal_expense can publish.
Refund/income/transfer/shared intent cannot become a positive personal expense.
V2 reuses all deny tokens and patterns from the immutable, hash-verified v1
text-readiness policy, plus its existing refund and shared-expense extensions.
Every interaction checks both original source and current reply without literal
masking, independently of model intent or coverage labels. Quoted merchant or
description values do not exempt a known unsupported financial-event word.
The common Finance result validator reconstructs and verifies this deny-only
original text from the bound raw intake or complete normalized local OCR blocks,
after the existing source/revision proof. This includes new edits to compatible
ORIGINAL, HUMAN and legacy-AI cards. Card-edit provider input keeps source_text
empty; the private local text adds no model evidence ref or monetary/date
authority. Missing or changed original custody fails closed. Status, publication
and lineage rebuilding use the same validator; v1 readers remain unchanged.
At most six unique-field set operations affect amount/currency/transaction_date/
merchant/description/category. Non-date operations use operation_id/op/field/
value/evidence; date operations substitute date_expression for value. Money is a
positive canonical decimal string checked for currency precision. Merchant is
legal/nonempty; description/category may explicitly be null. No actor/account/
ledger/approval/finalization/tool instructions are allowed.

Evidence lists contain bounded exact quoted strings/codepoint offsets and
source/reply/context roles. Previous monetary values must match the actual card
or verified draft. “Not20, change to15” can therefore distinguish old/new roles;
unresolved choices clarify. Unknown signs/currencies and illegal precision remain
blockers. Initial receipt amount respects genuine hash-verified TOTAL roles,
not subtotal/tax/cash; an authenticated reply can deliberately change a proposal.

Initial receipt interpretation also preserves the actual current HUMAN monetary
override. An old source TOTAL cannot revert it, and a CASH figure matching that
override cannot become the original TOTAL. This conflict requires an authentic
new monetary answer; fresh card edits/followups may deliberately propose a new
value and still require a newly delivered complete card and confirmation.

Coverage spans are ordered/nonoverlapping and cover current instruction text:
initial source_text; card edit/followup new reply_text. They reference operations/
questions and label resolved/unresolved/non-instruction/confirmation-request.
This proves structural coverage, not perfect semantic understanding. Known
unhandled financial requests or unresolved questions prevent a confirmable card.
The full card and change summary expose the interpretation for human review.
Reject duplicate/unknown keys, nonfinite values, bool/float money and bad quotes.

Date tags: absolute_date(year,month,day); day_offset(days -366..366);
calendar_week_weekday(week_offset -1/0/1,iso_weekday1..7);
previous_or_next_weekday(direction previous/next,iso_weekday1..7), strictly
excluding today. Weeks start Monday. Python computes from verified source time
for initial input or authenticated reply observed_at for edits/followups, in the
fixed timezone. Delay/replay cannot use processing-now. Ambiguous partial dates,
conflicting choices or unsupported expressions clarify. Omitting a requested
date cannot erase its conflict from whole-instruction readiness checks.
One V2 cue policy, including the existing conservative v1 cues and the complete
supported day-offset vocabulary, serves initial omission, alternative arms,
reply requests and unhandled remainders. All existing weekday aliases and full
English month formats retain their bounded evidence grammar. Date/time labels
request a date on all three paths but are not date-value evidence. A known
initial request cannot be answered by a source-context/default operation; it
needs a current source replacement, subject only to the existing unchanged
complete-ISO source-date exception. Numeric offsets require complete
unsigned integer tokens: a 3-day operation cannot cite the suffix of 13 days,
a decimal offset or a signed offset. Only verified date characters are excluded
from money checks, leaving negative amounts and currency conflicts visible.
The shared cue layer recognizes signed/decimal shapes inside
the same day-offset frames as unsupported date obligations; it cannot accept
their value, drop their date question or mask their characters. Qualified OCR
TOTAL money evidence does not exempt other source date requirements.
Existing explicit field literals, quoted values and month-name merchants retain
their exclusions; an unquoted merchant cannot swallow a separate date request.
For initial exact source replacement fields, a shared finite classifier may
exclude only a month word in an ordinary ASCII phrase. Unnamed description or
category also needs an independently supported complete source date; an
unquoted `at May Cafe` merchant retains its bounded name exception. Numeric or
ordinal month/day neighbors, weekday/relative/request words and a same-clause
date label cannot gain this exception through a shortened model value. The
finite 1–31 English cardinal/ordinal guard does not calculate dates. Existing
bounded date alternatives are checked before this weak month masking, so
neither a descriptor nor a broad evidence quote can erase their conflict.

## Durable clarification and followup

prepare_initial(intake_id,operation_id) creates v2 understanding.
prepare_edit(review_id,reply_evidence_id,operation_id) accepts actual delivered
PostingReviewv2 without an extra amendment card; compatibility inputs are
explicitly versioned. prepare_clarification(attempt_id) returns request ID/hash,
question projection and expiry from authenticated NEEDS_CLARIFICATION.
get_clarification(request_id) returns OPEN/CONSUMED/STALE/EXPIRED/CANCELLED.
The question binds source/currentproposal/version/hash/partialresult and has
confirm_available=false; it does not require posting-ready fields.

Questions include every unresolved Core financial obligation as well as useful
model questions. A date-only question or answer cannot drop an unresolved money
or currency choice. An unqualified replacement amount and its unknown currency
require an authentic unambiguous answer for that monetary pair. Qualified partial
fields may survive unrelated questions. The `context` evidence always describes
actual `current_values`; it never changes meaning to a proposed draft. Retained
context or the existence of a set operation cannot discharge a still-unresolved
amount, currency or date. A fresh reply may explicitly choose the old value.
All six pending fields use one fresh-content predicate: an exact reply citation
must support that field's selected value, not merely carry a replacement role.
Reply text-literal ownership comes from the authenticated reply's existing
top-level merchant/description/category assignment syntax, independently of
omitted, mismatched or shortened model operations. Nested field labels and
money/date/keep/clear words inside those values cannot answer another field or
create unrelated questions. The actual outer request remains visible. The same
raw-offset ownership view serves pending answers, ordinary edits, date evidence,
money checks, retention and optional clear; initial-source date grammar is
unchanged. A text answer must match its complete top-level assigned value, not
a nested same-field label or a shortened part of that value. An identical value
outside a literal needs its own exact outside citation. Unsupported answers
retain their pre-patch canonical value and outstanding question across followups;
independently supported partial answers still carry forward.
The existing three named assignment heads and quoted `at` merchant head each
create a same-field reply obligation even when the model omits that field's
operation. Every assignment for the same field must be closed and agree with
the selected complete value. A conflicting duplicate assignment, keep instruction
or optional clear cannot discharge that obligation; consistent repeated values,
a supported single assignment and an unopposed explicit keep remain valid.
Amount/currency/date support must survive the existing explicit text-literal and
verified date masking. Currency needs an explicit current-reply currency code;
a bare symbol inherited from context cannot settle its pending choice. Text
fields need a same-field named assignment outside another field's assigned
literal in the actual reply; its fresh citation may cover only the assigned
value. An exact bare value can answer only one pending text field when no
monetary/date cue makes that answer ambiguous.
Ordinary explicit currency-code replacements also require an exact citation
covering the matching currency token outside text literals. An identical code
inside a literal cannot borrow support from a separate outside token. Existing
ordinary bare-symbol and context-only currency retention semantics are unchanged.
Bounded keep/retain/保持/保留 plus the same field name may choose its actual
current nonnull value. A second same-field instruction or conflicting money/date
cannot hide behind that choice. A retained date masks only its verified field
cue. These finite choices do not give context or another field's reply authority
to answer a pending question; unanswered fields remain in the question ledger.
For description/category, a set/null operation can answer an outstanding
question only with exact replacement evidence from the new authenticated reply
that explicitly names and clears that same field. The same predicate governs
explicit edits: remove/clear/delete or 删除/清空/移除 must attach to the current
field-name cue in the actual reply, and one fresh exact citation must cover the
whole clear instruction. Explicitly assigned text literals cannot supply that
authority, even when another operation is omitted or selects an incomplete or
mismatched value. Quoted literals remain isolated across punctuation, newlines
and escaped quotes; an unclosed quote conservatively isolates the remainder.
This includes existing quoted `at` merchant literals without a matching merchant operation.
Unquoted literals stop at bounded clause separators or the next named-field
instruction. Genuine field-first instructions such as `category: clear` and
`description：清空` retain the same support. Clearing another field,
retained/source/context evidence,
omission or an implicit “clear it” cannot answer the question. Required
amount/currency/date/merchant null remains blocked; clearing optional fields
cannot discharge other outstanding obligations. An unsupported optional clear
retains its canonical pre-patch value in the clarification draft and leaves its
question unanswered. Omitted optional fields that
were not questioned retain their existing values.
An explicit null operation may also retain a genuinely null current optional
value as a no-op when all evidence is exact context/retained_value, the field
has no pending question and the current instruction requests no change to it.
This exception cannot use a partial draft's null, source evidence or an unrelated
reply replacement, and cannot answer a pending or explicit field request.

These are finite lexical safeguards, not proof of arbitrary natural-language
understanding. Unlisted or ambiguous date expressions require clarification;
known unsupported-intent words remain deliberately conservative, including
within otherwise legal merchant names.

prepare_followup(request_id,reply_evidence_id,operation_id) proves actual SENT
question, same principal, exact reply linkage and current frontier, then creates
one successor. It is a newly requested interpretation, not provider retry.
Migration059 adds immutable clarification requests/followup links with unique
generation/predecessor/successor/reply identities. Successor and reply consumption
commit atomically; replay returns the same attempt and concurrency has one winner.
Only an expired but otherwise current/unconsumed question can be manually
refreshed from the same result, without model dispatch. Old generations, changed,
cancelled, posted or consumed targets reject replies.

058 initial/edit kinds remain unchanged; v2 sealed material and actual059 edges
distinguish card edits/followups. Verify full schema and both physical proposal
and interaction frontiers. Only sealed NEEDS_CLARIFICATION plus a fresh authentic
reply can continue; UNKNOWN/timeout/unsealed observations cannot retry. Final
ancestry revalidates every predecessor source/input/provider/result.

## Full-card authority and guarded facts

full_card_ai_expense_v1 assessment binds current canonical six fields, actual
deterministic financial projection, source/interpretation digest, hard blockers
and review warnings. Missing/ambiguous required money/currency/date, invalid
merchant/fields, unsupported intent, bad evidence and incomplete display block
confirmation. Low OCR and resolved historical flags remain preserved warnings,
not a perpetual six-HUMAN barrier on this new basis. AI fields stay AI-derived;
whole-card confirmation is separate human authority, never fake field witnesses.

PostingReviewv2 hashes assessment/fullprojection/source/currentversion/payer/
expiry. Real authenticated confirmation must bind the actually SENT complete
card. Change-and-confirm only creates a new card; unseen values cannot already
be approved. A change invalidates the old card. Preparing/publishing AI material
alone authorizes no final fact. One builder serves prepare/submit/accepted/recovery.

Fixed typed ReceiptPostingAuthorization revalidates real accepted attempt,
display/decision, source/target, provider ancestry, payer and calculated projection
inside the converter's actual transaction. Converter builds its own expected
identity and compares every field. AI lineage, metadata callbacks, bools, empty
maps or caller-selected verifiers cannot replace this authority. Confirmation,
conversion/replay, fact-set/snapshot, conditionalfinalization and readback all
reverify the accepted basis. Old rules remain for old basis. Existing immutable
Posting JSON/decision/evidence retain v2 authority; only guarded conversion/
finalization creates facts and recovery preserves one approved economic event.

V2-origin text conversion has a separate fixed `TextPostingAuthorization`,
defined in `finance_core.application.text_posting_authorization`. The neutral
`decision_owner.convert_confirmed_parser_proposal` and
`decision_owner.verify_converted_parser_proposal` entry points accept the
keyword-only `text_posting_authorization` argument. `None` keeps the old call
shape source-compatible, while verified v2 ancestry makes this authority
mandatory at runtime, including when accepted review rows are missing. V1-only
proposals retain their existing path.

`PostingService` builds the capability for its exact attempt through
`_text_kwargs(attempt_id)` and passes it to both entry points. The gate runs on
the owning financial connection and active snapshot, reconstructs the expected
proposal, source, accepted decision, displayed review, full projection and
eligibility identities, and asks the fixed Posting owner to reauthenticate the
actual accepted source/display/reply/decision evidence. AI ancestry remains a
separate required proof. Stored review hashes are structural checks, not human
authentication; caller-supplied callbacks, dictionaries, booleans and forged
proof objects cannot substitute for the fixed capability. Direct create,
idempotent replay and result readback all use the same gate.

## History, atomicity and acceptance

Creation proof -> ordered revision reconstruction -> current admission -> receipt
qualification has single-direction responsibilities. Historical readability is
not new action authority. Row/dict are equivalent; marker inventory reads also
support tuple rows without changing the caller's connection configuration.
Real OCR/audit owners retain
concrete rejection reasons. Preserve original source, seals, field origins and
audit continuity. Provider wait is outside writer transactions, a durable claim
has one dispatch winner, and status/recovery never dispatch. Signed mirrors and
complete new publication graph verify before commit; local failure rolls back
all publication writes while the earlier external outcome stays durable.

Every reserved independent-AI marker on the actual ancestry must have its exact
creation, revision or publication owner. This includes outgoing publication audit
and lifecycle records on a HUMAN or legacy-created parent. A coherent audit hash
chain and the presence of the expected event do not excuse orphan, surplus,
duplicate, misplaced or contradictory events. Local owner proofs do not invoke
current admission to prove creation. The publication audit also binds its exact
event type, published-at seconds converted to canonical UTC with the financial
audit owner's normalization, and null calculation snapshot ID/hash. Equivalent
time inputs normalize identically; stored audit timestamps remain canonical.
Local owner proofs do not invoke
current admission or posting qualification; full historical and live operations
retain their own semantic, provider, source and frontier checks. A cached review
cannot bypass revalidation of this history. Unchanged field evidence inherited
through genuine HUMAN revisions retains its actual provenance.

Creation domains come from verified incoming records, not parser labels or
identity prefixes alone. Legacy and independent results share the `air_`
namespace: only the exact native creation proof can claim a legacy occurrence.
A HUMAN receipt revision may preserve its parent's parser pair and OCR contract
under its own sealed correction link. Successive HUMAN revisions preserve their
actual current field evidence; they do not reset it to a distant AI creation.
These inherited markers and genuine outgoing publications still require exact
owners. Unknown, duplicated, relocated or contradictory markers remain errors.

Complete card, accepted decision and recovery use the same canonical Unicode
financial projection. This normalization never rewrites original source strings,
model response bytes or the exact quoted evidence used to verify understanding.

Synthetic SDK acceptance covers initial/edit/clarification/fullcard once-post,
legacy/mixed history, monetary/date conflicts, tamper, race, stale, UNKNOWN,
crash/replay and direct-bypass refusal. Preserve001–058. Wheel/sdist-derived
installed flows and exact member/resource/ledger proof precede final specialist
reviews and authoritative hosted validation. Source/installation proof does not
activate real providers/transport or publish a release.
