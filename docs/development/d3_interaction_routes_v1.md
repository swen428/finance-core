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
The older `capture(kind=text)` name uses the same route transaction for new
authenticated ingress. Fresh text without authenticated context and ingress is
refused. An existing no-route capture can only return an exact saved result
when its raw fingerprint, context and job ingress identity match; replay never
creates a new route or parser proposal.

Core owns route precedence under the same capture transaction. It preserves the
original text and SHA-256 exactly; only a classification copy normalizes CRLF
and CR to LF. Other control or format characters, including NEL, Unicode line
separators and zero-width controls, become `control_refused`. A line beginning
with a D1 field/reference label, or text containing a `d1card_` marker, is a
card candidate. D1's structural parser must accept all six supported fields
within its 16,384-byte evidence limit before the candidate becomes
`whole_card`; partial, malformed, mixed card/guided, extra-line, duplicate or
unsupported content is refused. Any ASCII/full-width equals sign or
standalone `完成` is a guided candidate, even with an unknown field or malformed
value. A guided update is actionable only with one ASCII equals sign, a known
field, one nonempty logical line and a safe value of at most 1024 UTF-8 bytes.

A historical guided message is checked against its original append-only request
and pending or settled result. The route's Telegram message lookup key and the
guided edit's proposal/version execution key are distinct. A different field,
value, execution key or completion text is refused instead
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
operation, field and field-value material. Guided updates also store the
deterministic D1 compatibility operation separately from the Telegram message
lookup key. `get_interaction_route` retrieves
it read-only under the same authenticated context by original message ID or
operation key, including after a guided session ends. `get_status` also
returns capture and processing status for a known intake/job, but does not
expose the route's actor, conversation, or field-value material without the
authenticated route lookup.

Text adoption rejects payloads that also contain Telegram media fields before
any persistence; those attachments require the separate receipt capture path.
An expired guided session cannot claim a newly arriving ordinary expense.

A worker may run `process_capture_job` only for `initial_intake`; for text it
creates the deterministic parser proposal after adoption. For `whole_card`,
`guided_update`, and `guided_complete`, a later worker must pass the frozen
material to the existing D1/guided authority commands. Each new business write
checks route kind, original message/context and operation material inside its
write transaction. Their validation, expiry, and idempotency decisions remain
authoritative. `control_refused`
requires a refusal/status reply using the saved reason and creates no parser
proposal. A job's `captured` state says only that evidence and routing were
saved. It does not say an edit or economic event succeeded.

Migration 055 rejects direct parser-output and AI-attempt inserts for any new
Telegram text intake without an `initial_intake` route, including the older
one-step Python intake API with no capture job. At upgrade it freezes the set
of existing Telegram text intakes already bound to a matching parser proposal.
Only those historical sources may continue their current proposal lineage or
prepare AI fallback under its existing eligibility rules; old unparsed sources
remain blocked. The admission set is immutable after migration. Older pre-055
databases retain their historical behavior. Its route table
uses `WITHOUT ROWID` so hidden SQLite
row identifiers cannot replace frozen evidence; primary, message, and operation
key collisions are also refused before SQLite conflict replacement executes.

The same gate covers every source-to-proposal binding direction: a Telegram
text source cannot change its identity, adopt a pre-existing parser by raw
pointer update, or accept a parser whose source identity is assigned later.
The initial intake route remains required for new parser and AI work. Migration
055 separately snapshots only guided updates already pending with one matching
request event. Recovery without a route must match that immutable snapshot and
the current pending material; a newly inserted request event or an old-looking
timestamp cannot create historical authority. New guided requests and
completions require a frozen route at both the Python call and the session or
event write boundary. Normal settlement of an admitted pending update may
clear it after recovering the existing result.
An admitted old text source may advance to its direct parser child when the
child retains the same source identity, or when a sealed AI fallback result
links that child to the admitted source and current parent. This preserves
pre-055 fallback recovery without admitting an unrelated proposal.
Even an `initial_intake` route cannot bind a Telegram text raw record to a
parser with a different source type or identity. Migration 055 permanently
seals parser IDs linked to Telegram text or AI fallback. The seal's restrictive
foreign key blocks SQLite `REPLACE` by database ID, including negative IDs;
insert and update guards protect public identities. Staging write connections
must keep SQLite foreign keys enabled for this protection.

D1 whole-card writes require the saved card route, original reply bytes,
message/context, card generation and operation identity. The Core transaction
checks this even when an internal caller omits its optional extra validator;
SQLite also rejects direct insertion of unauthorised reply evidence or
accepted/refused/noop operations. Guided edits that use a D1 card require a
matching guided route and pending request, or the exact migration-time pending
admission. The synthetic one-field card is checked against the guided field
and value rather than compared to the original Telegram text. Existing D1
operations replay by their original evidence and do not create new writes.
At cutover, a re-delivered guided message is matched to its persisted request
before the generic card/control shape rules. This also covers an old guided
value that happens to contain a card marker, another equals sign, or a value
accepted by the earlier text normalization rules. The saved field and value
remain authoritative; changed text is refused. New messages use the D3 grammar.

New receipt captions resembling a card, guided command or ambiguous control
are refused before intake publication with a request to send the instruction as
text. Exact replay of an already saved receipt compares its original caption
without reclassification. OCR text is never treated as a user command.
Migration 055 does not rewrite attachment evidence or older jobs.
