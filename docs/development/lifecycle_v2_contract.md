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

Head B validation strengthens recovery and retained-history checks. Exact committed
operation retries are resolved before current-card expiry and latest-proposal
checks. They authenticate the original action/review/operation and verify the
original fact/decision plus that record's retained commit inventory; they return
the original receipt even after a later correction. A different runtime binding,
actor, action, review or operation cannot borrow that result.

Complete audits enforce both directions between retained capture/proposal/review/
delivery/confirmation events and their owned rows. Every native confirmation maps
to exactly one fact version and decision; every decision/fact maps back to its
exact event, operation, review, action, hash and sequence. Historical import
completeness additionally uses the independently authenticated retained archive
union. Ordinary reads inspect only the selected record's indexed history; the
complete inventory runs on startup, daily audit, snapshots and restore checks.
These checks detect missing owned rows against retained evidence. They cannot
detect rollback of **all** relevant state to a previously valid complete prefix
without an independent retained checkpoint. No external commit anchor is added
or claimed by this candidate.

Both live-original promotion and signed isolation receipt issuance use the same
proof: actual FENCED state, released original lifetime owner, exact current
record/version/hash and audit-sequence cut, and admitted installation/schema
identities. A stale cut remains RESTORE_HELD. To inspect the fixed original
profile while the caller's environment names the new held profile, this proof
holds the existing process-wide managed SQLite lock, temporarily selects only
the trusted original runtime path, closes its read owner, and restores the
caller's environment in `finally`. Tests cover contention, stale rejection and
exact-cut promotion, including restoration of the new runtime environment on
both success and failure.
