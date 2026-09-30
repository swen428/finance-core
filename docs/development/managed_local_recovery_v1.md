# Managed local recovery and processing status (v1)

This component admits the existing `get_capture_recovery`,
`resume_capture_recovery`, and `get_ai_processing_status_v2` commands on an
already enrolled synthetic staging profile. It does not acquire attachments,
perform OCR, invoke providers, send replies, authorize a restored profile, or
produce a complete backup. Ordinary temporary-staging request shapes and
financial authority remain unchanged.

## Caller and saved source

Managed processing status requires `workspace_path`, `intake_public_id`, and
`operator_actor_id`, `telegram_account_id`, `telegram_conversation_id`,
`conversation_binding_id`. Shape validation precedes target lookup. Core
matches this complete caller context to the persisted raw intake's frozen
Telegram context, canonical positive message ID, channel, external source
identity and recomputed identity digest before returning processing state.
Missing or wrongly bound targets are unavailable; their existence is not
revealed. These fields are trusted caller inputs, not authentication of a
Telegram update. The installed Host/Bridge must supply authenticated context.

Status may describe early, failed, admission-denied, pending, unknown or
finished processing without a proposal, OCR result or AI attempt. It does not
claim work, retry a model or create a human decision. Existing status services
remain the authority for what is actually saved.

Recovery retains its original complete caller context, saved capture source,
frozen route and step token. A token selects a saved view; it is not consent.
The current view is read again before acting, and a stale token returns the
current view with a no-op. Source and original service authority checks still
apply to each actual action.

## Connection ownership and local actions

Each local phase uses a short workspace-owned session through actual connection
close. If saved correction history needs local authority, the probe session
closes before the controlled correction factory opens. That factory verifies
its exact workspace, policy and actor and supplies the original correction
verifier. Arbitrary caller connections cannot become registered authority
connections. A changed state at the transition requires a fresh verified view
or refusal, never an unverified old amount.

Local initial/child review preparation, accepted posting recovery and existing
result enqueue use the original services and transactions, with source guards
for the persisted proposal/card/review/attempt they actually select. They do
not manufacture confirmation or a second financial event. Frozen whole-card
and guided-edit replay reuse private in-session helpers shared with the
original public handlers; parameter, frozen-route, revision, signature and
replay checks remain binding. No handler recursively opens another managed
session while holding the current one.

The shared profile gate excludes an exclusive backup cut for the operation's
lifetime; it does not exclude other lawful shared writers. Original service
transactions and authority rechecks remain responsible for their financial
concurrency rules. A source guard is not a new atomic transaction spanning all
recovery actions. This component does not wrap those services in a blanket
transaction or blanket commit.

`capture_processing_required` explicitly refuses before processing claim,
attachment access/publication, engine construction, OCR or provider work. Its
pending state remains readable. `capture`, `propose` and `process_capture_job`
remain refused in the managed dispatcher. Existing A review/edit receipt
restrictions are unchanged; supported local B receipt facts can be checked
without acquiring original image bytes.

## Evidence and completion boundary

Synthetic tests exercise the three actual public entries, early source-bound
status, missing/mismatched source refusal, local action and stale/replay
behavior, original/corrected result recovery, frozen edit replay without nested
sessions and acquisition refusal before effects. Relevant ordinary-staging
recovery/status behavior remains covered. An error after an original service
commit leaves the returned result unverified; query or replay the durable
identity rather than infer that no write occurred.

Dispatcher inventory is 39 admitted and 3 refused database commands, plus the
unchanged pure compatibility verifier. These counts do not establish installed
Bridge/Host integration, attachment completeness, migration or full-cut
acceptance. Complete backup acceptance requires all enabled owners and the
separate full manifest/readback verification.
