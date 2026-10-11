# Independent AI invocation and proposal contract v1

This retains the pre-release v1 interpretation/historical-reader contract.
New independent invocations use [v2](independent_ai_invocation_v2.md); v1
records do not authorize new v2 calls or full-card posting.

The independent Application AI boundary owns one durable, bounded text
interpretation and its unconfirmed parser proposal. It does not own a network
provider, runtime credentials, human confirmation or final-fact authority.
Python remains the monetary authority. Existing platform compatibility receipts
retain their original domains and cannot qualify a new independent invocation.

## Fixed composition and authenticated evidence

Composition fixes the SQLite connection, independent `TrustedBinding`, source
verifier, interpretation-input verifier, provider observation authority, policy
and schema identities, clock and deadline. Operational methods accept stable
references, never a caller-selected verifier, actor, endpoint, model or key.

Import `AiInvocationService` from `finance_core.application.ai_invocation` and
the fixed port types from `ai_invocation_contract`. The service operations are
`prepare_initial(intake_id, operation_id)`,
`prepare_edit(review_id, reply_evidence_id, operation_id)`, `claim(attempt_id)`,
`invoke(attempt_id)`, `record_outcome(attempt_id, observation_record_id)`,
`publish(attempt_id)`, `cancel(attempt_id)`, `get_status(attempt_id)` and
`recover(attempt_id)`. `invoke` calls the constructor-owned provider port;
status/recovery do not call it. The provider port returns a durable observation
reference and reauthenticates it through `verify_persisted`.

An interpretation-input verifier proves the retained source, exact delivered
review/card, current target and authenticated reply text. It supplies no human
patch or approval authority. Provider observation references resolve through
the fixed provider port against the exact claim, request and observed
attribution. A caller-constructed observation DTO is insufficient evidence.
Publication and recovery revalidate the original input and observation.

Selected text is bound to the retained authenticated intake bytes and content
hash. A pending parser's copied `raw_text` cannot substitute a different
source. Every mutating entry requires the connection's foreign-key enforcement
before its writer transaction; it never silently enables it or alters a
caller-owned transaction to proceed.

Provider waiting occurs outside a SQLite writer transaction. A dispatch claim
is committed before outbound work; only its fresh winner receives permission
to send. Duplicate calls, restart and recovery return durable status rather
than a second dispatch permission. No automatic retry, provider/model fallback,
tool use or expansion of allowed data is part of this contract.

## Text-only input and strict output

Initial input contains only the bounded, verified source text or local OCR text
and its opaque source references. Edit input adds the authenticated reply and
the exact current six-field projection needed to interpret it. It excludes
unrelated conversations, ledger contents, credentials, filesystem paths and
generic content/tool blocks. Original images, derived PNGs, thumbnails, image
URLs and base64 attachments are never model input. OCR failure cannot trigger
a cloud-vision fallback. Sensitive text is refused before dispatch.

Privacy checks inspect the actual selected source, reply and current-field
strings before JSON serialization, so escaped whitespace cannot hide a
credential pattern.

The versioned output is a constrained personal-expense proposal, a six-field
patch or an explicit clarification/unsupported result. Reject duplicate or
unknown keys, non-JSON and oversized responses, non-finite values, booleans or
floating-point amounts, invalid date/currency/precision, tool/ledger/actor
targets, and approval/finalization fields. One invalid patch member refuses
the whole patch. Monetary strings are canonicalized by deterministic Python
currency rules; model confidence cannot establish money or erase ambiguity.

An amount-only edit may inherit the exact current card's already explicit,
valid currency. A currency symbol without that known context requires
clarification. Income, transfer, refund and shared-expense interpretation must
not silently become a personal expenditure. Failed and rejected raw responses
are retained boundedly; logs and user messages expose safe reasons and opaque
references, excluding credentials, headers and raw provider exceptions.

`InterpretationPolicy(timezone, default_currency)` is required trusted
composition, with no built-in consumer-specific defaults. The exact policy is
bound into invocation material and its text projection. Source-date context is
derived from the verified source timestamp in that fixed timezone. A default
currency can qualify only absent explicit conflicting or ambiguous currency
evidence; it cannot clear an unknown symbol. Context citations must bind the
exact supplied value. Neither model output nor an operational request can
select a timezone/default currency or replace that original policy.

The versioned text-readiness assessment independently checks the actual source
or authenticated edit reply for competing amounts, currencies and dates and
unsupported intent. A matching quoted token alone cannot resolve competition.
For an eligible text proposal with no identified restriction, the old deterministic
parser's aggregate score is retained as history rather than treated as a new
AI confidence score. Explicit low-confidence/conflict evidence remains a
restriction. This rule does not apply to receipt OCR confidence or its guards.

Every authenticated edit reply receives the text conflict/intent checks even
when the original source is a receipt. Initial OCR also rejects explicit
unsupported financial intent; its total-role and multi-number interpretation
remain governed by the existing receipt owner.

These are finite checks of identified conflicts, not proof that Python has
understood every natural-language sentence. AI supplies a fallible personal
expense interpretation; the existing fresh HUMAN decision approves the whole
eligible card before guarded facts. Known conflicts cannot be waived by a
model-selected field, citation, or human confirm.

The versioned policy rejects the refund family `Refund`, `Refunds`, `Refunded`
and `Refunding` in initial text, initial OCR intent, and authenticated replies.
Actual refund posting remains unsupported; no refund type or relative-date
conversion is introduced. The model must return existing `unsupported` or
`clarification` kinds when appropriate. Core retains its raw reason in the
response and exposes the existing safe state/reason; it does not turn that
reason into authority or claim an actual Telegram prompt was delivered.

For initial text and authenticated edits, a literal ISO date offered as an
alternative to an existing recognized date cue stays unresolved even when
the model quotes only the ISO or omits the date patch. In either direction,
the ISO must directly adjoin an existing alternative connector apart from
whitespace; the other local arm is searched for the cue without interpreting
its remaining words. Neighboring connectors, semicolons, line breaks and
sentence punctuation bound each arm. Periods within ASCII decimal or dotted
date tokens do not split it. A cue in another clause cannot supply the arm,
and no natural date is converted. Pure ISO dates and description
words such as `May Cafe` or `October special` remain distinct. The existing
source-context date-cue refusal is unchanged: bare month, partial and relative
date cues do not gain default-date support. Initial OCR date geometry,
confidence and ambiguity remain with its original receipt owner; this text
alternative check is not a claim of general OCR natural-date interpretation.

Initial receipt amount and currency must match the same unique total-role
pair reconstructed from the original versioned receipt owner and hash-verified
OCR layout. Cash, subtotal and tax figures cannot substitute for that pair;
missing or conflicting totals stay pending. A current human monetary override
cannot be overwritten through initial interpretation. A deliberate monetary
change uses the authenticated edit reply and a fresh complete card.

Positive expense support includes the original monetary sign and boundaries,
not merely a quoted numeric substring. Negative or unsupported accounting
notation cannot be reinterpreted as a positive amount. The same qualification
is rebuilt at result, publication and historical verification. Normal dates,
positive totals and non-total discount roles retain their existing meaning.

Source qualification partitions the retained raw text before selecting any
amount. Supported monetary numbers are literal ASCII unsigned decimals;
deterministic Python still checks positivity and the currency's precision.
Unknown signs, number forms, separators or currency notation refuse
qualification rather than restart extraction at the next digit. Compatibility
normalization can detect suspicious notation but cannot rewrite source bytes,
evidence offsets or quotes into accepted input. Multiple money occurrences
remain ambiguous even when they have the same value.

Currency qualification distinguishes supported explicit currency, proven
absence and present unsupported or ambiguous notation. Only proven absence
can use the fixed default currency; an unknown code or Unicode currency symbol
cannot become absence. An amount quote must cover a number span already
qualified in the complete source, so quoting only the digits cannot hide an
unsupported sign. Every genuine receipt TOTAL candidate is checked against
its original, hash-verified role; a second unsupported TOTAL cannot disappear
through deduplication of equal parsed amounts.

An ambiguous three-letter merchant token is not proof that unknown currency
notation is absent. Such a receipt can require clarification even when its
TOTAL is otherwise valid. Genuine explicit human input can use the existing
amendment owner; unchanged fields in its canonical patch are not material
field witnesses. The resulting full projection still requires a fresh human
posting decision. AI failure never supplies that decision or silently permits
a consumer to fall back to a confirmable old parser card. The consumer must
implement its nonconfirmable repair-card route separately.

## Durable outcomes and publication

Preparation binds the source/current proposal, operation identity, exact text
selection, policy/schema/prompt and nonsecret provider configuration. Reusing
an operation with different material refuses. Claim has exactly one winner.
A response alone creates no proposal, decision or fact.

Cancellation before claim has no dispatch. Cancellation after claim and a lost
or timed-out external result remain unknown about remote execution; never
report that no external work occurred. Late observations may add knowledge
about that original attempt but cannot become timely publication permission.
Wrong provider/model, invalid response, stale target and cancelled attempts
preserve evidence and do not publish. Recovery performs no network call.

Valid timely output is published only after rechecking source, current target,
configuration and event-wide confirmation/conversion state in an owning
transaction. Each material initial interpretation or edit creates one genuine
immutable independent-AI child. The parent, input, raw response, validation,
complete resulting projection, field origins and publication edge are bound
together. Leaf replacement, receipt links and audit append commit atomically;
the full source, provider observation, publication graph and current status are
verified before commit, so a failed final verification rolls back all local
publication writes while retaining the previously committed invocation outcome.
Replay returns the original publication
without moving a later current leaf backward.

Historical status and recovery reconstruct the captured source revision from
the actual original parser payload and continuous, authenticated HUMAN
completion records. They revalidate the retained source, original edit
review/reply, exact request and fixed provider observation; a cached payload
is not revision authority. Legitimate later human completion does not make
an old invocation unreadable. Corrupt or incomplete revision evidence refuses
instead of being reported as merely stale.

Historical legacy AI edges are verified from their immutable original creation
link, claim, result, source, field evidence and creation audit. Genuine later
HUMAN completions are separate versioned proofs. These local record readers do
not call current-domain admission or reconstruct the whole mixed graph again;
current admission still requires the real current intake route and full fixed
provider authority. An old compatibility receipt proves only its original old
invocation and never authenticates a new independent provider observation.

An unpublished invocation cannot claim, record an outcome, cancel or publish
against an obsolete current proposal, version or content hash. Historical
VALID or unclaimed PREPARED results report RESULT_STALE when their target
changed. Pending or uncertain historical outcomes retain their original
meaning; stale recovery creates no new event or dispatch. For a genuine
already-published invocation, authenticated claim/invoke/publish replay returns
its original publication without writes or a second provider call; its
is_current value reflects the actual current leaf. Historical read permission
never grants permission for a new publication or final fact.

Receipt children carry the distinct `independent_ai` OCR link and genuine field
evidence. They preserve original text/image/normalization/OCR identities,
confidence and restrictions. No new AI child masquerades as a legacy fallback,
D1 correction, human completion or human amendment.

## New card, fresh human decision and guarded facts

An AI edit invalidates the old review. The complete new card must be actually
delivered before a fresh exact human reply can authorize posting. A reply that
asks for a change and confirmation cannot approve values it has not yet seen.
Clear confirmation, denial and status remain deterministic human actions.

The full mixed AI/human ancestry is verified, including every independent
invocation, authenticated observation and exact publication. Missing, forged,
changed or unsealed ancestors refuse review, amendment, posting and recovery.
Independent AI ancestry requires its fixed composition authority; static
records or a caller-provided no-op callback cannot supply that authority.
Direct lower-level conversion/finalization cannot bypass the owning path.

The dedicated `AiLineageAuthority.verify_in_snapshot(connection, expected)`
protocol receives the actual financial snapshot and Core-built exact source,
binding, proposal, version and content hash. Its authenticated projection must
match the independently reconstructed lineage. A generic metadata callback,
boolean result or empty dictionary is insufficient. New-origin detection
cross-checks parser/publication, receipt-role, field and lifecycle evidence;
partial schema or contradictory markers cannot downgrade the origin.

AI interpretation supplies no `VerifiedHumanAmendment` and writes no human
amendment record or HUMAN field witness. Receipt qualification after incomplete
or low-confidence OCR keeps the existing requirement for six material,
current-value-matching, genuinely human witnesses. AI-filled fields cannot
exercise that exemption or borrow a witness for a changed value. A blocked
receipt remains pending with an explicit reason and needs genuine field input.
A material AI overwrite removes the prior HUMAN binding for that field; a
later AI return to the old value cannot restore it. A new genuine material
HUMAN transition can establish a new binding, while unchanged fields preserve
their existing authenticated HUMAN authority.

Receipts originally missing currency or date may be genuinely HUMAN-repaired
and eligible for guarded posting while subsequent AI edits still require
clarification, including unrelated edits. The original restrictions remain in
AI readiness. Such a requested edit is not applied: consumers must retain its
pending evidence and must not silently fall back to confirming the old card.
This contract does not grant an AI-readiness exemption from verified HUMAN
repair or claim universal successful natural editing after that repair.

Only the existing guarded posting/conversion/finalization path produces final
facts after the fresh human decision. Before that step final tables remain
empty; replay and crash recovery preserve one economic event and the approved
receipt snapshot. Source delivery and synthetic tests do not activate real
providers, transport, credentials, a production writer or a published release.

## Installation and validation

The additive migration and every new policy/schema resource are part of the
exact packaged member inventory and migration ledger. Existing migration
bytes and old compatibility identities remain unchanged. Wheel and
sdist-derived installation must expose the new feature surface outside the
checkout, with source/member/RECORD/resource evidence; a package version alone
does not identify this feature.

Focused acceptance includes genuine initial and natural-edit posting, receipt
link/source checks, unchanged human receipt qualification, mixed ancestry and
tampering, duplicate dispatch/restart/cancel/UNKNOWN/late/stale outcomes,
text privacy and injection refusal, and clean installed import/ledger proof.
These precede final specialist reviews and exact-candidate hosted validation.
