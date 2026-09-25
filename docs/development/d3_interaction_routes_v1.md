# D3 durable interaction routes v1

Migration `055_d3_interaction_routes.sql` adds one append-only route for each
text message adopted through `capture_interaction`. The route is source/work
evidence, not confirmation or financial authority. A matching raw intake,
authenticated Telegram source context, capture job, and route commit in one
SQLite `synchronous=FULL` transaction. Capture returns before parser, AI,
editing, confirmation, finalization, or reply delivery.

`capture_interaction` accepts `workspace_path`, exactly one validated
`telegram_update` or `telegram_message`, the four authenticated source fields
(`authenticated_actor_id`, `telegram_account_id`,
`telegram_conversation_id`, `conversation_binding_id`), and the required
`finance_ingress` object documented in [D3 capture jobs](d3_capture_jobs_v1.md).
Its idempotency key is the existing
`raw-intake:telegram:<chat_id>:<message_id>`. It uses the existing text intake
identity and job derivation, then returns `interaction_route` and
`final_transaction_created=false`. The caller must independently check the
ingress digest and source fields against the host's original update before
using any capture as a host adoption receipt. A replay first verifies the
original content fingerprint, authenticated source context, ingress digest,
and saved route. It never reclassifies from the current session. An old
capture with no route cannot be treated as a successful interaction adoption.

Core owns route precedence: D1 whole-card shaped text first, historical
guided message identity, unexpired current guided session, then initial intake. A
malformed card or guided control is `control_refused` with a queryable reason;
it is never sent to the ordinary parser. The immutable route stores original
text SHA-256, message/context identity, and applicable card, session,
operation, field and field-value material. `get_interaction_route` retrieves
it read-only under the same authenticated context by original message ID or
operation key, including after a guided session ends. `get_status` also
returns the route for a known intake/job.

Text adoption rejects payloads that also contain Telegram media fields before
any persistence; those attachments require the separate receipt capture path.
An expired guided session cannot claim a newly arriving ordinary expense.

A worker may run `process_capture_job` only for `initial_intake`; for text it
creates the deterministic parser proposal after adoption. For `whole_card`,
`guided_update`, and `guided_complete`, a later worker must pass the frozen
material to the existing D1/guided authority commands. Their validation,
expiry, and idempotency decisions remain authoritative. `control_refused`
requires a refusal/status reply using the saved reason and creates no parser
proposal. A job's `captured` state says only that evidence and routing were
saved. It does not say an edit or economic event succeeded.

Migration 055 rejects direct parser-output and AI-attempt inserts tied to a
saved non-intake route, so an older worker cannot silently process that
message as a new expense.

The pre-existing `capture` command remains compatible for initial receipt
images. Migration 055 does not rewrite attachment evidence or older jobs.
