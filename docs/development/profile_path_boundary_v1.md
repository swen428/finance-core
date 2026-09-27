# D4 profile path boundary (public Core)

This public component validates an **existing** profile layout. It does not
create or activate a real profile, open a database, authorize a restored file,
or produce a backup. The private macOS owner is responsible for provisioning
the profile tree and selecting the user's Application Support directory. Core
receives that directory and a bounded profile ID explicitly; it never infers
them from the checkout or an inbound message.

The expected layout is
`<Application Support>/Finance-Codex/profiles/<id>/` containing `profile.json`,
`runtime/database/`, `workspace/database/`, `backups/`, `work/`, and `restore/`.
All directories *below* Application Support must be owned by the current user
and mode `0700`. `profile.json` and any existing database files must be regular,
single-link, owner-owned `0600` files. `profile.json` must bind `profile_id`,
the absolute canonical `runtime_root`, and the sibling `workspace_root`.
`FINANCE_RUNTIME_ROOT` must exactly name that runtime directory. The reserved
live DB path is `runtime/database/finance.db`; the staging path is
`workspace/database/staging.sqlite`, outside the runtime root. Existing
staging-guard authorization still applies to every database open/write.

`validate_profile_paths(application_support_root, profile_id)` returns a
context-managed `ProfilePaths` witness and rejects repository roots, symlinks,
hard-linked files, unsafe ownership or permissions, mismatched roots and
unexpected path changes. It also refuses nonregular files without blocking on
FIFOs and, on macOS, refuses extended ACL allow entries even when mode bits
look private; deny-only ACLs remain valid. Public path fields are read-only and
come from the same immutable mapping that `revalidate()` checks. Keep the
witness open and call `revalidate()` just before each path-based operation. A
file that appeared after validation needs a new witness. This is a path and
permission check, not a capability to open or mutate arbitrary data. The
eventual backup and restore adapters must pin and recheck their own file
descriptors around actual I/O and apply the existing staging/restore authority
checks.

D4 tests construct only disposable synthetic trees under temporary paths.

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
Node process and cut session. It never creates a profile or initializes a
missing gate. Its wait and cooperative hold limits are bounded; the lease is
checked at each export transition and is released after protected cleanup.
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
