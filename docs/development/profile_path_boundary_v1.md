# D4 profile path boundary (public Core)

The original `validate_profile_paths()` boundary validates an **existing**
profile layout and permits only blank reserved database paths. It does not
create or activate a real profile, open a database, authorize a restored file,
or produce a backup. A separate P1 managed synthetic-staging component is
described below; it does not change the original witness contract. The private
macOS owner is responsible for provisioning the profile tree and selecting the
user's Application Support directory. Core receives that directory and a
bounded profile ID explicitly; it never infers them from the checkout or an
inbound message.

The expected layout is
`<Application Support>/Finance-Codex/profiles/<id>/` containing `profile.json`,
`runtime/database/`, `workspace/database/`, `backups/`, `work/`, and `restore/`.
All directories *below* Application Support must be owned by the current user
and mode `0700`. `profile.json` must be a regular, single-link, owner-owned
`0600` file. `profile.json` must bind `profile_id`,
the absolute canonical `runtime_root`, and the sibling `workspace_root`. For
Core Python profile validation and runtime access, `FINANCE_RUNTIME_ROOT` must
exactly name that runtime directory. The reserved
live DB path is `runtime/database/finance.db`; the staging path is
`workspace/database/staging.sqlite`, outside the runtime root. Existing
staging-guard authorization still applies to every database open/write.

This validator accepts **blank profiles only**: both reserved database names
must be absent. An existing live or staging database, including a symlink,
hard link or FIFO at either name, fails before the validator opens any regular
file. The absence is checked again during validation and on each
`revalidate()`. This witness cannot authorize a writer, backup, or restore. A
separately registered managed staging witness is required for the narrow P1
staging component below. The validator never opens or closes an existing
database descriptor, which avoids disturbing SQLite's process-level POSIX
locks.

`validate_profile_paths(application_support_root, profile_id)` returns a
context-managed `ProfilePaths` witness and rejects repository roots, symlinks,
hard-linked files, unsafe ownership or permissions, mismatched roots and
unexpected path changes. It also refuses nonregular files without blocking on
FIFOs and, on macOS, refuses extended ACL allow entries even when mode bits
look private; deny-only ACLs remain valid. Public path fields are read-only and
come from the same immutable mapping that `revalidate()` checks. Keep the
witness open and call `revalidate()` just before each path-based operation. A
database that appeared after validation invalidates the witness; getting a new
witness cannot admit that populated profile. The manifest stays pinned and is
rechecked by its descriptor and named identity without a fresh open. This is a
path and permission check, not a capability to open or mutate arbitrary data. The
eventual backup and restore adapters must pin and recheck their own file
descriptors around actual I/O and apply the existing staging/restore authority
checks.

D4 tests construct only disposable synthetic trees under temporary paths.

## Managed synthetic staging component (D4-3 P1)

P1 adds a separate, fixed-path witness for an enrolled **synthetic staging
database**. Enrollment uses `bootstrap_registered_staging()` on an existing
blank `ProfilePaths` witness; `verify_registered_staging()` checks the same
explicit Application Support root and profile ID. Bootstrap uses the
existing staging factory and complete temporary-database migration manifest;
it never accepts a caller-selected database path or authorizes
`runtime/database/finance.db`. If the fixed profile gate is missing, bootstrap
initializes that single fixed gate. It does not create profile directories or
adopt a pre-existing database.

The managed registration is `.managed-staging.v1.json`. It binds the profile
ID, runtime/workspace roots, fixed staging path, migration-contract digest,
staging main-file device/inode, and a new instance ID. Registration publishes
through `.managed-staging.v1.pending`; a leftover pending name marks an
incomplete publication and remains evidence. Missing, malformed, copied, or
mismatched registration; an unregistered main database; an unexpected fixed
sidecar; or an unsafe file role fails closed without deleting or rewriting the
observed database or registration. Registration is cooperative custody
evidence for the fixed staging instance, not a profile issuer or defense
against the excluded same-user offline-mutation threat.

Every managed SQLite open acquires the profile's shared gate before SQLite can
recover or create a sidecar, then keeps the lease until rollback and actual
connection close finish. The fixed main and its `-wal`, `-journal`, and `-shm`
roles are checked against the enrolled profile and owner-only file rules;
stock SQLite remains responsible for ordinary hot-journal and WAL recovery.
If connection close is uncertain, the component retains both the connection
and shared lease until that process exits. A waiting exclusive cut must not
pass before the process has exited and been reaped.

The generic public `open_staging_database()` refuses the fixed profile staging
path regardless of enrollment state. Reopening that fixed path is available
only through the scoped managed operation while its shared gate is held;
ordinary temporary staging paths retain the existing generic factory behavior.

The separate delivery-receipt consumer and Bridge `health` and `get_status`
commands use short managed database sessions. Five identity-bound local reads
(`get_interaction_route`, `list_capture_recovery_candidates`,
`get_capture_job_for_message`, `get_guided_edit_session`, and
`get_human_draft_card`) and thirteen local interaction/review/edit commands
also use short managed sessions. The thirteen are `capture_interaction`,
`get_review`, `confirm`, `edit`, `reject`, `issue_human_actions`,
`redeem_human_action`, `apply_guided_edit_update`, `complete_guided_edit`,
`apply_human_draft_card`, `begin_human_draft_card_delivery`,
`record_human_draft_card_delivery_outcome`, and `reissue_human_draft_card`.
Each retains its existing identity, signature, replay and financial authority
checks and service-owned transactions. The session remains held through query
or local transition completion and actual connection close. Draft delivery
commands only record local attempt/failure/unknown state; they do not send or
accept a trusted success receipt. The caller derives the enrolled profile from
the fixed workspace layout and configured `FINANCE_RUNTIME_ROOT`, not from a
request flag or arbitrary database path. Every other Bridge database command
targeting the managed layout still refuses before its handler can open the
database or perform an external effect. Ordinary staging workspaces retain
their existing route.

On a managed profile, `get_review`, `confirm`, `edit`, `reject`, and
`issue_human_actions` require `operator_actor_id`, `telegram_account_id`,
`telegram_conversation_id`, and `conversation_binding_id`. Core matches them to
the proposal's frozen Telegram source and verified proposal lineage before
returning review content, issuing actions, replaying a result, or recording a
decision. These
request fields do not authenticate a Telegram update: the Host must authenticate
the update and pass its context through the Bridge. Existing Bridge review and
decision calls omit that complete context, so they refuse on managed profiles
until the later caller-integration work is delivered. The ordinary temporary
staging request format remains unchanged.

`get_ai_processing_status_v2` remains in that refused group because its existing
request does not carry the authenticated Telegram context required to isolate
another conversation's intake.
The Bridge commands include gate acquisition and session close in their
cooperative command deadline. An error after an admitted command's service
transaction commits, including a close/deadline error, leaves its result
unverified; the caller must query or replay its durable operation identity,
not infer that no change was written. The same rule applies to a receipt
consumer error after its existing posting authority commits: inspect or replay
the receipt token.
This partial integration does not admit the `capture`, `propose`, or
`process_capture_job` attachment/OCR paths, correction, migration, or any full
backup cut.

The enrolled database component's resource inventory is `profile.json`, the
fixed profile gate, the managed registration and pending name,
`workspace/database/staging.sqlite`, and its three fixed SQLite sidecar roles.
The admitted `get_status` path may also read a canonical receipt original to
report its existing integrity state; it does not acquire, publish, or back up
attachments. This slice has no cut-worker protocol or Host state. Synthetic
tests use child processes for crash/reopen and gate-overlap fixtures.
The managed receipt test proves session routing with a synthetic authority
stub; the existing D2 authority tests cover authenticated commit directly.
An end-to-end managed receipt commit/replay fixture is not established by this
slice.
`receipt_staging_runner`, the
remaining Bridge commands, correction adapters, and migration or
restored-profile entry points are not integrated with this managed API. Those
entries remain outside this component's proof. A passing component test is
evidence for only the
enumerated Core slice; it does not close all D4-C01–C04 acceptance or establish
D4-3 completion, a complete backup cut, a restore, or production readiness.

The legacy path-taking `migrate_database_safely()` and
`create_verified_backup()` entries refuse the fixed managed profile namespace,
including backup output there. The direct backup entry also checks the caller's
actual SQLite main path, rejects attached, memory, temporary, closed or
mismatched connections, binds the preflight target to that main path, and
refuses a copied or renamed staging authorization before producing output.
For a staging database copied alone to an ordinary
path, the legacy migration entry can inspect its existing authorization only
after its normal SQLite open; that check precedes backup creation and schema
mutation, but is not a pre-open, sidecar-free guarantee. These refusals grant no
managed migration, backup publication, or restored-profile authority.

## Fixed profile gate (v1)

The profile gate is an opt-in interprocess coordination primitive for
participating durable writers and a future short backup cut. It is not a
database lock and does not make nonparticipating writers safe. A writer must
hold a shared lease for its entire durable write; an exclusive cut excludes
those shared leases. There is no shared-to-exclusive upgrade. Every writer
that must be excluded by a cut has to adopt this gate explicitly.

### Initialization and profile precondition

Initialize the fixed entry `.profile-gate.v1.lock` explicitly, after validating
the existing profile. Core callers pass the live `ProfilePaths` witness to
`initialize_profile_gate(profile)`. The Bridge's
`initializeProfileGate(profileRoot)` accepts a path string and checks the final
directory and lock entry; its caller remains responsible for validating and
pinning the full profile and ancestor boundary first. Initialization creates one
empty, owner-owned, single-link regular file with mode `0600`, then syncs the
file and containing profile directory. Normal opens never create it. An
existing entry is never replaced or repaired implicitly. If initialization
fails after creating the entry, it preserves that entry in a mode that normal
opens refuse.

Core writers use `writer_gate(profile)` for a shared lease, and a future
consistent cut uses `exclusive_cut(profile)`. Bridge callers use
`openProfileGate(profileRoot)` followed by `acquireShared(...)` or
`acquireExclusive(...)`. The lock file's name and inode are fixed for the
profile; callers must close the gate and every lease using the provided
close-only lifecycle. Closing the lease's owned descriptor releases its OS
lock. Do not issue `LOCK_UN` on a descriptor shared with a forked child: it can
release the parent's lock too. Do not unlink, replace, or recreate the gate to
release or recover a lease.

### Wait and hold bounds

Core `writer_gate` and `exclusive_cut` default to a five-second wait and reject
waits longer than 30 seconds. `writer_gate_from_parent` is a single immediate
attempt. Bridge acquisition takes an integer wait from 1 through 30,000
milliseconds. Exclusive leases in both APIs have a maximum 30-second hold
bound; Core accepts a finite positive duration, and Bridge accepts an integer
`maxHoldMs` from 1 through 30,000. Callers may request a shorter bound. Core's
`GateLease.assert_valid()` and Bridge's exclusive lease `assertValid()` check
the monotonic deadline and pinned file identity when called. Expiry is
cooperative: it does not asynchronously interrupt or release a long-running
operation. A future exporter must check the lease before each cut transition,
abort on a failed check, and close the lease in a `finally` path. Shared writer
leases do not have an automatic hold deadline, so callers must keep them only
for the durable write they protect.

An expired check prevents the cut from continuing but does not implicitly
release an already returned exclusive lease. The exporter closes it after its
protected cleanup, so writers stay excluded through that unwind. Acquisition
that expires before lease handoff closes its untransferred descriptor.

Acquire the exclusive cut only after network, OCR, and provider work has
finished. Do not hold either lease while waiting on those external operations.
Acquire a shared writer lease only for the local durable-write section. Because
the Bridge caller retains its shared lease until the child is reaped, attach
one only to a bounded child operation that performs that write without external
waits; split out network, OCR, or provider work before entering the gated
section. Keep the exclusive section limited to the local consistency work that
requires writers to pause.

### Child writer handoff

A Bridge shared lease can be passed to a child as FD4. `bindChild()` reserves
the caller-owned lease before spawn; the runner calls `unbindChild()` after the
child has closed and been reaped. The caller must keep its lease open until
then. FD4 is an identity witness for Core, not proof that the parent still
holds its lock. A child that writes must independently call
`writer_gate_from_parent(profile, inherited_fd=4)`: Core validates the witness,
opens the fixed entry through a new descriptor, and makes exactly one
nonblocking shared-lock attempt. Contention fails immediately; the child does
not reuse or upgrade the inherited descriptor. The parent must retain its own
shared lease through child reap so an exclusive cut cannot pass between the
child's handoff and its write.

### Current capability boundary

This gate does not by itself produce a complete, consistent backup. The public
Bridge owner-state exporter described in
[`bridge_owner_export_v1.md`](bridge_owner_export_v1.md) freezes only the
Bridge handoff portion of a D4 cut. It does not back up or restore SQLite
databases, prove that every runtime writer participates in the gate, admit or
activate a real profile, or change a consumer's fixed Core version. Only paths
that explicitly acquire the gate participate in its coordination. Existing
staging-guard authorization still applies to every database open and write;
the gate does not relax it. A complete D4 cut must combine the Bridge export
with the other approved, independently validated cut components.

## Bridge owner-state export

`exportBridgeOwnerState({ applicationSupportRoot, profileId, runtimeRoot },
options?)` is an explicit operation on one already provisioned profile. It
validates the fixed profile layout, opens the profile's existing gate, and
freezes the Bridge handoff tree while holding one exclusive lease in the same
Node process and cut session. It does not read or require `FINANCE_RUNTIME_ROOT`;
Core's Python profile/runtime boundary above continues to require that setting.
It never creates a profile or initializes a missing gate. Its wait and
cooperative hold limits are bounded; the lease is checked at each export
transition and is released after protected cleanup.
The profile owner must separately provision an empty `workspace/handoff/` and
its fixed handoff lock before backup, even before the first receipt. The path
validator alone does not require these Bridge-owned entries; missing entries
make this exporter fail closed until private profile wiring supplies them.

The exporter is an owner-state boundary, not the whole D4 backup protocol. It
returns the frozen Bridge handoff description, a manifest entry containing the
staged file's relative name, byte size and SHA-256, and the private stage path.
The manifest proves the staged bytes' identity. It does not prove database
consistency, whole-profile completeness, restore safety, or permission to
promote or activate the stage.

Pending and reclaim state is part of the evidence. A recognized standalone
pending record (`.finance-bridge.record.pending`) is copied as found and
represented as `incomplete`; export does not publish it into a completed slot.
Reclaim intent and its related files are likewise retained until a separately
authorized Core custody check proves reclaim is safe. A final handoff record
whose image is missing is rejected unless a matching reclaim intent explains
the state. A valid `.finance-bridge.payload.pending` does not change that rule:
D3 must finish its recovery/replay before owner export can proceed. Export must
not resolve, discard, or label an incomplete handoff complete.

Unknown handoff entries, malformed records, invalid or mismatched content
hashes, missing required images, and unsafe file identities fail the export.
The exporter leaves any created stage available for owner inspection on
failure and does not return a success result. A successful stage also does not
auto-promote: a separate complete D4 acceptance step must validate all cut
components and explicitly decide whether any stage may be finalized.

D4 Bridge tests use only synthetic profiles and temporary files. They verify
the successful frozen manifest and fail-closed cases without opening live
profiles, live databases, or real owner data.
