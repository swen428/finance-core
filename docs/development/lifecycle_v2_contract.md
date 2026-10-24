# Lifecycle v2 offline contract

This opt-in API is an offline-synthetic feasibility candidate. It does not admit
production bindings, publish a release, replace historical facts or activate a
runtime. Migration 061 appends new tables; migrations 001 through 060 are unchanged.

`LedgerService` requires the actual `ProductionOwner`, a fixed `RuntimeBinding`,
and a trusted `EvidencePort`. Capture authenticates retained input and original
bytes, including the source timestamp and Asia/Singapore interpretation zone.
Provider requests bind exact source, actor, generation, current proposal/review,
predecessor values, and authenticated edit input. The existing independent v2
response validator and `Money` contract remain monetary authority.

The lifecycle is capture -> propose -> prepare_review -> record_delivery ->
confirm. A delivered card and human action must refer to the same exact review
hash and actor. Reviews expire after ten minutes. Corrections create new versions
of the same economic event; unique source/event constraints prevent duplicate
posting. Exact same-operation retries recover persisted results; a different
operation cannot reuse confirmation rights. Successful receipts say LOCAL_POSTED.

The current reader uses indexed records, independent of completed transport
history. It verifies referenced source/provider/publication/human-decision
receipts, canonical values, fact predecessors and audit binding. Complete audit
also covers unused proposals, pending reviews, all fact versions and signed
historical import archives. Imported pending interactions remain non-executable.

Each v2 protected operation verifies installed migration artifacts at entry,
then reuses only that immutable proof until actual SQLite close. Dynamic file,
owner, gate, current-authority, schema, transaction and close checks remain.
Legacy protected operations retain their original verification behavior.

`seal_snapshot` holds a read transaction while validating full history and
copying SQLite through its native backup API. `SnapshotCut` records the exact
record/version/hash set, audit sequence, database hash, and installation/schema
identities. Private composition must pause intake while capturing its journal
and originals into the same protected cut; this API alone makes no remote-copy
protection claim.

Restore creates a genuinely new instance in durable RESTORE_HELD. Ordinary
admission and current owners cannot open it. Promotion requires fresh approval
and either verifiable original FENCED authority or an independently HMAC-signed
old-writer isolation receipt. Such a receipt is issued only after the original
writer is actually fenced, releases its lifetime lease, and the exact cut is
compared with its actual database. The receipt permits recovery after the old
root becomes unavailable. It proves a previously recorded fence; it cannot prove
isolation of a machine lost before that fence. `resume_held_restore` reissues a
held capability after restart only against the signed cut and durable held state.

Impact: new lifecycle callers, trusted private evidence-port composition,
application schema inventories, migration resource manifest, snapshot manifests,
restore/admission state replay, and focused synthetic tests. Historical public
posting APIs and migrations remain available. Installed combination validation,
independent reviews, hosted validation, release and activation are separate gates.
