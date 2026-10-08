# Independent pre-confirmation proposal amendments

The Application amendment boundary accepts a verified human request to change
an existing personal text expense or total-only self-paid receipt proposal.
It changes a proposal, never approves it or creates financial facts. The
original message, attachment, parser/AI/OCR evidence and source-event identity
remain intact. Posted correction and external transport are separate owners.

## Trusted composition and operational references

Construct `AmendmentService` from `finance_core.application.amendment` with
keyword-only `connection`, `source_verifier`, `human_amendment_authority`,
`binding` and `clock`. `AmendmentBinding` wraps the existing `TrustedBinding`
and fixes a separate amendment namespace/key ID. Operations supply references:

- `prepare(proposal_public_id)` produces a complete `AmendmentReview` of the
  current proposal, its editable values, source, binding and expiry. This is
  an edit target, not confirmation permission; incomplete proposals remain
  visibly incomplete.
- `amend(review_id, amendment_evidence_id, amendment_id)` obtains the patch
  from the trusted durable authority and returns the published revision.
- `get_status(amendment_id)` verifies the historical result and reports whether
  its revision is still current. It does not republish or move the intake leaf.

Operational input cannot choose a principal, verifier, signing key, database,
patch authority or finalization path. `HumanAmendmentAuthority.verify_persisted`
reads the supplied SQLite snapshot and returns `VerifiedHumanAmendment` for an
internally constructed `ExpectedHumanAmendment`. Typed objects alone are not
authentication. The adapter must verify its durable source, successfully sent
full display, exact authenticated reply, integrity and configuration binding;
UNKNOWN sends and self-reported approval are insufficient. It cannot fabricate
historical OpenClaw, D1 or AI evidence. Core owns lifecycle and field validation.

The independent human-edit domain is `finance-application-human-amendment-v1`.
It is distinct from posting approval. The exact old proposal reference, version,
content hash, full display projection, source, principal, instance and action
must agree. An edit request containing a confirmation phrase still cannot
approve values that have not been displayed.

## Whole-patch validation and genuine publication owners

The only editable keys are `amount`, `currency`, `transaction_date`, `merchant`,
`description` and `category`. Existing Python Money, date, bounded text and route
rules apply. A mixed patch is one operation: any invalid or forbidden member
rejects all members. Currency changes cannot round an incompatible amount;
missing currency is not guessed. Unsupported intent, account, payer, allocation
or source classification cannot be changed through these six fields.

Only actual material changes select the publisher. Four-field nonmonetary
changes use the existing append-only completion owner. Real receipt monetary
changes use the existing receipt supersession owner and its genuine correction
link/revision evidence. Real text monetary changes use a narrowly scoped text
supersession owner with an independent human-edit lineage. A copied parser name
or synthetic D1 publication is not that lineage. There is no second monetary
payload overlay.

The revision identity is the proposal reference, version and effective content
hash together. A new child can start at version zero; a bare version number does
not establish currentness. The same intake/source event survives every edit.
Missing or corrupted publication, completion, lifecycle, audit or source/AI/OCR
ancestry fails closed. Unresolved ambiguity remains unresolved unless its real
owning evidence supports the supplied correction.

## Atomic authority, replay and a fresh confirmation

The owning SQLite `BEGIN IMMEDIATE` transaction verifies both fresh and replay
material. The publisher and the independent accepted edit/result edge commit
atomically. A failure before commit leaves no new revision, completion, consumed
edit or moved current pointer. A crash after commit recovers the same result.

A fresh edit requires a current unconfirmed target with no accepted posting or
conversion on its economic event. Confirmation winning the writer race prevents
an edit, including when final conversion has not finished. An edit winning first
invalidates the old leaf/version/content commitment, so an old confirmation
cannot approve the new values. Parallel edits of one base have one winner.

Exact replay verifies the accepted historical evidence and returns its applied
revision, even after a later edit, expiry or posting. It cannot reactivate an old
card or move the current pointer backward. Conflicting reuse of an operation or
evidence ID refuses. Historical verification uses acceptance-time proof; fresh
expiry/current-card rules do not invent a second action during recovery.

A valid edit requires a complete new display and a separate new accurate human
confirmation before normal guarded posting. Independently edited pending
proposals are eligible only with a fully verified independent edit lineage.
Arbitrary legacy edited states and generic human actors cannot inherit this
permission. Legacy D1/D2 owners retain their own authority contracts.

For a successfully extracted receipt with historical low confidence or an
undetermined merchant, [independent posting](independent_posting_v1.md) requires
material human provenance for all six current fields before allowing a new
posting review. Genuine inherited corrections/completions remain attributable
to their original publications and must equal the latest current values.
Echoes and normalization create no material provenance. Historical OCR
confidence/flags remain evidence; an edit never clears them or confirms posting.
Subsequent nonmonetary completions preserve inherited evidence bytes. The
conversion owner recognizes an older value as historical only after verifying
both its original owner and the later independent field-specific completion
on the exact current ancestry. Sequential completions retain each latest
material field witness, including through a subsequent monetary supersession.
Dates follow that same verified publication order: changing only amount or
currency cannot reactivate an older corrected date or invalidate the newer
independently completed date.

## Whole-expense description and category

Independent receipt description/category describe the whole expense. They do
not replace item descriptions, change allocations, infer a payer or alter the
Python calculation. The additive schema preserves existing rows with NULL
metadata; it does not reinterpret notes or backfill history.

For the supported independent contract, the reviewed metadata must be persisted
with receipt facts, bound to the actual snapshot identity and finalization
fingerprint, stored in the canonical transaction and checked on replay/readback.
A trusted owning metadata authority verifies the accepted review inside the
financial transaction. Channel names, booleans and callbacks alone cannot
turn the legacy metadata refusal into permission.

The independent projection composes the existing numerical receipt calculation
with a versioned bookkeeping extension. Finalization compares it with the real
snapshot and real confirmed receipt metadata. Comparing only totals or trusting
a prospective display is insufficient. Old metadata-free conversion, snapshot
and D2 hash shapes remain unchanged; new optional fields do not rewrite old
proofs. Text independent posting likewise preserves the exact approved expense
description/category in its verified canonical result.

## Schema, packaging and verification

Migration `057_independent_amendments.sql` is additive. Migrations 001–056,
existing releases and active consumer identities remain unchanged. New immutable
review/edit/text-revision records, FKs, uniqueness, collision protection and
schema identity must agree with the actual publisher and lineage reader.

The migration contract, release-manifest validator and installed Bridge verifier
must all admit exactly 001–057. Missing, extra, reordered or modified resources
refuse. Generated Bridge provenance binds the changed verifier. A fresh
wheel/sdist installation outside checkout verifies real packaged bytes; a local
source import alone does not prove an installed combination.

Focused validation covers six fields, mixed invalid patches, chained edits,
missing values, stale/wrong/expired/UNKNOWN display and reply evidence, replay,
concurrent edit/confirmation, interruption at owner commit boundaries, immutable
source and AI/OCR lineage, genuine text/receipt posting and exact metadata/snapshot
readback. Existing legacy compatibility and cold dependency/import guards remain
required. Three independent final-diff specialists and exact-head hosted
validation precede explicit owner approval for protected merge. Source delivery,
release publication, active consumer upgrade, installed transport acceptance and
real runtime use remain separate results.
