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

Core owns route precedence under the same capture transaction. It preserves the
original text and SHA-256 exactly; only a classification copy normalizes CRLF
and CR to LF. Other control or format characters, including NEL, Unicode line
separators and zero-width controls, become `control_refused`. A line beginning
with a D1 field/reference label, or text containing a `d1card_` marker, is a
card candidate. D1's structural parser must accept the whole candidate before
it can become `whole_card`; malformed, mixed card/guided, extra-line, duplicate
or unsupported content is refused. Any ASCII/full-width equals sign or
standalone `完成` is a guided candidate, even with an unknown field or malformed
value. A guided update is actionable only with one ASCII equals sign, a known
field, one nonempty logical line and a safe value of at most 1024 UTF-8 bytes.

A historical guided message is checked against its original append-only event;
a different field, value, operation key or completion text is refused instead
of being reinterpreted under the current session. An unexpired active guided
session owns a new non-card message, accepting only valid guided syntax;
malformed syntax is refused. At `expires_at <=` the capture-time clock, an
expired session does not claim a genuinely new ordinary expense. With no active
session, any guided candidate is refused. Only after all control candidates
and ambiguous characters are excluded can text become `initial_intake`. This
conservatively refuses some ordinary notes containing labels or equals signs.
The pure control-shape classifier is also the receipt-caption admission input;
OCR text is never interpreted as a user control command.

A malformed card or guided control is `control_refused` with a queryable reason;
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
message as a new expense. Its route table uses `WITHOUT ROWID` so hidden SQLite
row identifiers cannot replace frozen evidence; primary, message, and operation
key collisions are also refused before SQLite conflict replacement executes.

The pre-existing `capture` command remains compatible for initial receipt
images. Migration 055 does not rewrite attachment evidence or older jobs.
