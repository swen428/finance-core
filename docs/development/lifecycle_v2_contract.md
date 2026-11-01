# Lifecycle v2 offline contract

This opt-in API is an offline-synthetic feasibility candidate. It does not admit
production bindings, publish a release, replace historical facts or activate a
runtime. Migration 061 appends new tables; migrations 001 through 060 are unchanged.

The root `finance_core.API_CONTRACT_VERSION` remains `finance-core-api-v1` because
its existing exports and behavior are unchanged. `finance_core.lifecycle_v2` is a
separately named, opt-in additive API; consumers must explicitly import it and
bind the installed package, API marker and 061 migration ledger together. This
source change alone assigns no new immutable release or active consumer identity.

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

`LedgerReader.source(source_id)` accepts the retained lifecycle source ID and
returns a frozen `VerifiedSource` containing authenticated original input, its
hash, actor/client, evidence identity, timestamp and interpretation timezone.
Native sources are checked against the trusted evidence port and original capture
event; imported sources require the independently trusted legacy verifier and
signed retained source capsule. Missing or inconsistent evidence is refused.
Archived-generation sources remain readable; this read grants no current proposal
or confirmation authority and creates no lifecycle decision or fact.

New native captures retain an exact `lifecycle-capture-v2` event envelope with
`format`, original `generation`, and the original `source` DTO. The source DTO,
`source_sha256` (the digest of that DTO), provider request identity and public
`EvidencePort` remain unchanged. Point reads, complete audit and exact capture
replay require the envelope's generation to equal the source row, even for an
archived read. A changed generation column cannot borrow current initial-write
authority. Malformed/unknown envelopes cannot fall back to historical parsing.
Both supported capture formats require exact canonical JSON, rejecting duplicate
keys and alternative encodings of the same parsed object.

Historical plain-DTO capture events retain their original bytes and source hash.
They remain readable and auditable as **generation-unbound history**; their
standalone row generation is not proof of original capture authority. They cannot
authorize a new initial proposal, initial review or first confirmation, including
an already pending initial card. Exact capture replay returns only the original
reference and never upgrades or rebinds the event. Already committed facts remain
readable, auditable and eligible for fresh explicit corrections under the current
binding, with the usual authenticated provider, delivery and human evidence.
An exact committed confirmation retry still observes its original result. A
retained delivery record or proposal reference by itself grants no first-fact
authority. This compatibility restriction must be considered before any separate
consumer upgrade; no automatic recapture, event rewrite or authority backfill is
provided. Independently signed legacy-import capsules keep their existing rules.

The envelope detects inconsistency between a source row and retained capture
material. It does not add an independent authenticity root for generation:
coordinated rewriting of the row, capture and ordinary event chain, or rollback
of their complete state, still requires an external trusted checkpoint to detect.
Historical plain events cannot retrospectively prove their original generation.
Migration 061 and all historical migration bytes remain unchanged.

Each v2 protected operation verifies installed migration artifacts at entry,
then reuses that proof only through its own active connection witness. The proof
binds the exact config, owner, process and session; it is not an ambient context
flag and cannot authorize another operation, copied context, child or owner.
Dynamic file, owner, gate, current-authority, schema, transaction and close checks
remain. Legacy protected operations retain their original verification behavior.

`seal_snapshot` holds one read transaction while validating the complete retained
history, schema and migration ledger, measuring the complete logical database
and copying SQLite through its native backup API. The logical digest includes
all main tables, old posting tables, declared application tables, storage types,
hidden rowids and SQLite sequence rows. It is not an economic-equivalence hash:
a legitimate later legacy posting or other covered content change invalidates
the cut even when the lifecycle fact watermark does not move. SQLite page/WAL
layout is not part of this logical digest.

The destination must be new and under a trusted, caller-owned private 0700
directory without expanded ACL permissions. It is reserved exclusively as a
regular, single-link 0600 file, without following links. Parent and file identity
remain pinned through copying, actual close, independent readback and fsync.
Existing files, dangling links, hard links, unexpected sidecars and detected
replacement fail closed. Failed partial output stays private and is not a
successful cut. SQLite still opens its target by pathname; this guarantee
depends on trusted private-directory custody and cooperative maintenance, and
does not claim an fd-based VFS or protection against malicious same-UID races.

`SnapshotCut` uses `format="lifecycle-snapshot-cut-v3"` and
`logical_digest_format="core-logical-database-v1"`. It retains the exact
record/version/hash set, audit sequence, installation/schema identities and
closed snapshot file's raw `database_sha256`, and requires
`logical_database_sha256` and `source_binding_sha256`. Independent closed-target
readback must match the source logical digest and pass schema, ledger, integrity
and foreign-key checks. `SnapshotCut.from_dict` strictly decodes the complete v3
JSON shape; absent proof fields, old cuts and implicit upgrades are rejected.
The compact authority `cut_ref` binds the complete cut hash and fixed digest
identities without copying the potentially large fact list into the control log.

Private composition must pause intake while capturing its journal and originals
into the same protected cut. This API alone makes no remote-copy protection claim.

Restore first prepares a concrete new instance and generation in durable
RESTORE_HELD, using `prepare_restored_profile(..., new_binding=...)`. Its target
identity includes the complete trusted config binding, runtime binding, held
database inode and exact preparation history entry. Ordinary admission and
current owners cannot open a held instance. A Python object or a successful
`verify_held_profile_evidence` diagnostic grants no future admission right.

After the old writer is fenced and has actually released its lifetime owner,
`designate_restore_successor` compares the entire source cut against the actual
old database and records one exact successor for that old authority binding.
The `recovery_designate` action requires its own operation and approval reference.
Both live-original and receipt promotion consume this same durable designation.
Another operation cannot designate a second target, even with fresh approvals;
an exact operation/reference retry returns the original designation. A lost or
discarded target does not silently reopen successor selection. An admitted
successor can later perform recovery under its own new authority binding.

`RecoveryAuthority.issue_old_writer_isolation(old_config, restored, ...)` signs
the existing exact designation as `old-writer-isolation-v3`. Its material binds
the namespace, old binding and fence entry, complete target, cut and designation
entry. It contains no fresh retry timestamp or random value, so an exact retry
can recover the same receipt. Old v2 receipts and unbound target strings are not
accepted. The target must be prepared before receipt issuance; a consumer cannot
sign for a name, lose the old root, and later construct a different target.

Promotion requires fresh approval plus the trusted `port` and exact new `binding`.
Under the same target owner/control/profile leases and SQLite transaction, it
rechecks the full cut, installed identity, schema/ledger and complete evidence,
including historical signed archives. It writes the pending transition, rolls
back its read-only use of the transaction and confirms actual SQLite close
before publishing ACTIVE. Cooperative leases remain held through publication.
A close-unknown result retains the pending state and uncertain resources;
ordinary admission cannot use that state. Prior held diagnostics never substitute
for these final checks.

Recovery actions retain strictly validated, action-specific `recovery_context`
in the existing hashed authority history; ordinary legacy history retains its
original shape. This is a control-log extension, not a new SQLite migration.
Generic authority resume cannot complete recovery designation/admission without
their dedicated full validation. Interrupted preparation must likewise retain
the exact v3 preparation context and recheck the target cut before publication.
`resume_held_restore(config, cut, receipt, recovery_authority)` reconstructs the
same originally HELD target without changing authority. A receipt-free live
caller may instead supply `old_config` by keyword; exactly one complete route is
required. The descriptor's advisory status is `RESTORE_HELD`,
`RESTORE_ADMIT_PENDING`, `ACTIVE`, or `FENCED`. Pending status takes precedence
over an ACTIVE disk state while its journal remains. Reconstruction authenticates
preparation, full target/file identity, designation and persisted route context;
only clean HELD reconstruction runs the held content diagnostic. Pending and
completed reconstruction grants no content or admission proof. Status may become
stale and never controls publication.

`resume_restore_promotion` repeats the protected content and evidence checks for
the caller's exact pending operation, approval and route. A completed retry
only observes its original entry/current state: it creates no second admission,
does not force a later legitimately changed ACTIVE database back to the old cut,
and cannot revive a subsequently fenced instance.

A signed receipt permits recovery when the old root becomes unavailable after
designation and signing. It cannot prove isolation of a machine lost before
fencing or signing. The one-successor guarantee assumes one trusted, unrolled-back
old authority lineage, trusted configuration/approvals and cooperative local
locks. Cloning or rolling back complete roots/keys requires an independent
checkpoint and deployment trust outside this candidate. Off-machine copies,
key custody and complete machine-loss recovery remain separate acceptance gates.

Impact: new lifecycle callers, trusted private evidence-port composition,
application schema inventories, migration resource manifest, snapshot manifests,
restore/admission state replay, and focused synthetic tests. Historical public
posting APIs and migrations remain available. Installed combination validation,
independent reviews, hosted validation, release and activation are separate gates.

Exact committed operation retries are resolved before current-card expiry and latest-proposal
checks. They authenticate the original action/review/operation and verify the
original fact/decision plus that record's retained commit inventory; they return
the original receipt even after a later correction. A different runtime binding,
actor, action, review or operation cannot borrow that result.

Proposal/correction retries authenticate the retained original request and compare
its exact reply evidence, reason, expected version and operation. They do not
rebuild an old request from the latest record state. A matching old retry still
returns its original proposal/review after a later legitimate correction;
changed input requires a new observation and operation. Retained reply reads and
complete audits enforce the same raw-text SHA-256 condition as initial writes.

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

Full logical scans and complete evidence audits occur at explicit snapshot and
recovery boundaries. Ordinary point/history readers do not scan the whole
database. Boundary cost grows with retained data and may require deterministic
sorting; prior benchmark results do not establish the cost of this protocol.
The source contract defines required behavior, not a claim that a particular
candidate has passed its focused, installed, independent-review or hosted gates.
