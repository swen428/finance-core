# Independent single-entry posting

The independent Application posting boundary owns one personal text expense or
one total-only personal receipt from a precise human decision through guarded
posting and verified result recovery. Parser, OCR and AI material remain
proposals. A prepared review creates no final transaction, receipt facts,
calculation snapshot or finalization permission.

This internal entry is composed by trusted application code. It does not
register a Telegram bot, transport, provider, network tool or production
instance. All financial writes retain the existing staging-database and
foreign-key guards. Synthetic signed adapters prove the port contract in
tests; they are not production authentication.

## Composition and complete review

Import `PostingService` from `finance_core.application.posting`. Supply a
migrated connection with `sqlite3.Row` and fixed keyword-only
`source_verifier`, `human_decision_authority`, `binding` and `clock` arguments.
The caller owns connection lifetime. Write operations require no pending
caller transaction; a refusal never commits or rolls back caller-owned work.

- `prepare(proposal_public_id)` returns a `PostingReview` view containing
  `review_id`, `review_hash`, complete `projection` and `expires_at`.
- `submit_post(review_id, decision_record_id)` accepts the precise decision and
  advances its durable attempt.
- `resume_post(attempt_id)` continues an existing accepted attempt.
- `get_status(attempt_id)` reads and verifies durable state without posting.

`PostingStatus` identifies the attempt, state, verified transaction reference
when present and attention reason when applicable. Owning financial guards and
verification errors remain refusals; a failed call cannot be interpreted as
permission to retry with different authority.

Service construction fixes its SQLite connection, source verifier, human
principal and service-instance binding, human-decision authority and clock.
Operations take durable record references. Requests cannot select an actor,
key, database, adapter, verifier, module or an arbitrary approval dictionary.

A complete posting review binds the current proposal ID, version and content,
verified source, all visible financial fields, unspecified account and
ambiguity/inference information. A personal receipt additionally binds the
unique active self payer and a nonpersistent Python calculation: one total
item, the entire amount borne by that payer, zero to collect and no settlement
obligations. Invalid, incomplete, ambiguous or unsupported financial material
cannot become an eligible review. Independent receipt posting preserves
supported description/category fields through the versioned
`application_bookkeeping_metadata_v1` extension: the accepted review, receipt
facts, authoritative snapshot and canonical transaction all bind those exact
values. This extension describes the whole expense and does not change item
allocation or monetary calculation. Legacy metadata-free identity and hash
shapes remain unchanged, and legacy receipt conversion retains its existing
metadata restrictions. Text financial fields use the owning converter's
canonical representation; independent posting also persists and verifies the
accepted description in the canonical transaction while retaining its original
proposal/source evidence.

The posting commitment has its own versioned domain. The existing read-only
admission observation and its review hash retain their original read-only
meaning; they cannot approve a future receipt calculation. The durable frozen review
is immutable; returned projection data is not a transferable approval. Its
status-independent financial content remains verifiable
after the proposal's lifecycle moves from pending to confirmed.

Trust and operational identifiers must already be Unicode NFC; a noncanonical
identity is refused rather than rewritten. Display projections use the owning
canonical JSON normalization. Original source text/bytes and their digests remain
unchanged.

The decision authority reads durable evidence in the supplied SQLite scope.
Its `ExpectedPostingDecision.accepted_attempt_id` distinguishes a fresh action
from historical verification of an accepted attempt. In historical mode the
port verifies the delivered/current display and human ownership at the accepted
time from durable history; closing a completed card does not erase that history.
Core owns decision consumption. The frozen decision report records its unused
status at acceptance, not a claim that Core's accepted decision is still unused.
It authenticates the source, independent human action, exact current delivered
display, target/version/content and complete posting commitment. Typed port
results report verification; constructing a dataclass does not authenticate
anything. A submitter or model cannot approve its own proposal. The future
transport adapter must independently prove its sender, private conversation,
actual sent card and exact reply linkage before joining trusted composition.

## One accepted decision and one economic event

Fresh confirmation must match the configured instance, human principal,
source and decision domains/keys, exact current review and display, confirm
action and validity interval. Wrong or stale targets, uncertain delivery,
forged/legacy evidence, consumed decisions and a competing decision refuse.

The service holds an owning SQLite write transaction while it revalidates the
current proposal and evidence. Decision consumption, the recoverable attempt
and parser confirmation commit together. No consumed decision may exist
without its durable result path. Operational callers cannot provide a
confirmation bypass or replace the trusted authority port.

Stable identities bind the accepted source event and exact decision. Matching
amounts or merchants are not an economic identity: two separate source events
may produce two legitimate expenses; the same event cannot produce another
expense through a different proposal or repeated reply.

Confirmation records a decision. It does not yet mean that the final expense
exists. A result may only say posted after the owning financial service's
canonical result and complete evidence have been verified.

## Guarded receipt calculation

Receipt conversion, item/allocation facts and immutable calculation snapshots
remain owned by their existing guarded financial services. The Application
uses deterministic command identities and binds their results to the same
accepted attempt. Original source evidence is preserved.

Each unfinished owning write rechecks accepted source, decision and current
payer authority while holding its SQLite write lock. Snapshot preparation
also binds the Application's snapshot evidence inside that same transaction;
a refusal rolls back the new snapshot, calculation run and binding evidence.
Previously committed accepted decisions and receipt facts remain recoverable.

After the actual fact set and snapshot exist, Python rebuilds their financial
projection and requires exact equality with the approved receipt calculation.
The independent conditional-finalization proof binds the accepted review and
decision, original confirmation, active fact-set identity, exact snapshot and
reviewed/actual projections. It has its own authorization version. Existing
manual and D2 authority cannot be relabeled to satisfy it.

The finalizer checks the independent proof inside its guarded transaction,
and its replay/readback owners verify the same authority. Financial or
participant-authority drift fails closed with evidence; a stage marker or
matching transaction ID cannot replace that proof.

## Recovery and readback

An accepted attempt can continue after a restart without inventing another
human action. Fresh decisions must be unexpired and unconsumed. Recovery
verifies that acceptance was valid when committed, along with durable source,
decision/display integrity and the current authority needed for unfinished
writes. Expiry after acceptance does not turn the same authorized attempt
into a new decision. Changed configuration cannot inherit pending write
permission silently.

A crash after any owning service commit can leave the coordination stage
behind. Recovery verifies the committed conversion, facts, snapshot,
authorization or final result before advancing coordination; it never treats
an in-memory response as financial truth. Repeated status reads or returning
the canonical result to a reply owner cannot produce another transaction.

Completed result readback verifies its historical accepted proof and canonical
financial result. Missing, damaged or conflicting evidence refuses rather
than reporting a successful posting. The coordination row's transaction
reference cannot substitute for a missing conversion or finalization proof;
only a verified owning result supplies the returned transaction identity.
Correction-aware effective results are
a separate work package; this entry must not claim an original transaction
is current when unsupported correction material is present.

Actual capture/ACK, card send, natural-language parsing and reply-send UNKNOWN
belong to a transport adapter. This boundary supplies a stable verified
financial result for that owner; it does not claim to have delivered a reply.
Release publication, consumer dependency upgrade and real runtime admission
remain separate operations.

## Pre-confirmation amendments

The additive [independent amendment owner](independent_amendments_v1.md) supplies
verified six-field edits and fresh review/confirmation for the same source event.
An edited state alone is insufficient: posting requires its complete independent
lineage, and a changed current leaf/version/content invalidates an old review.
Confirmed or already accepted posting cannot be edited through this boundary;
posted correction remains a separate work package.

Successful OCR can leave an unparseable receipt whose original confidence and
merchant indicators remain historical evidence. The independent personal
receipt path may retain `low_confidence` and `merchant_not_determined` in its
complete review only when every current editable field has a verified material
human correction or completion on that same independent source ancestry.
This includes description/category. The latest genuine material witness must
match each current value; inherited witnesses survive a later valid partial
edit. An unchanged supplied value or system normalization adds no witness.
The original OCR, flags and confidence remain unchanged. Financial ambiguity
still requires its existing durable amount/currency/date resolution.

A later independent merchant/date/description/category completion can replace
an earlier human value while its inherited field evidence remains historical.
The receipt conversion owner first verifies the original evidence, then the
exact complete current payload, independent ancestry and latest field-specific
sealed completion, including its actual publication and audit history. A bare
completion row cannot excuse a mismatch. Amount and currency never use this
override; legacy receipt behavior remains governed by its existing evidence.

Failed, missing or partial extraction, unknown indicators, incomplete human
coverage and unsealed or conflicting evidence remain ineligible. Preparation
and acceptance revalidate the current exact lineage and complete values;
pending writes and accepted-result recovery/readback repeat that verification.
The existing immutable posting review binds the source, version/content and
all six values, without a new approval domain. Edits and eligible review
creation still produce no financial facts: a separately delivered current
review and fresh exact human confirmation are mandatory.

## Compatibility and validation

The independent path is additive. Historical D1/D2 source and approval proofs,
exceptions, conversion identities and financial guards retain their owning
contracts. The stable public API/Bridge envelope and existing runtime tools
are not enabled or replaced by this internal entry. Historical migrations
remain byte-identical; new durable authority uses an appended migration and
updated packaged migration-ledger identity.

Use only synthetic inputs and temporary staging databases. Validation covers
both routes before/after confirmation, wrong and stale bindings, competing
approval/source replay, SQLite transaction ownership and concurrency,
close/reopen at durable commit boundaries, projection drift, proof tampering,
verified result recovery and legacy financial regressions. The Application
dependency guard and a cold-process import must prove that the new entry
cannot pull a platform adapter into its financial ownership path.

Focused local evidence and applicable Python quality checks precede the
three independent financial/domain, persistence/audit and assurance/security
reviews. Exact-head hosted Python and both Bridge systems remain required by
the [public validation contract](release_validation_v1.md); merge approval and
landed validation are separate from candidate test success.
