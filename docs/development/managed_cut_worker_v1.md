# Managed Core snapshot worker (S2-B)

`runManagedCoreSnapshot()` is an internal Core component coordinator. It uses a
fixed Python worker to stage one registered synthetic Core database and a
separate fixed Python reader to verify the closed output. It returns a
component receipt only after both children have exited and been reaped and the
reader has checked the output. The receipt is not a complete profile backup or
a `cut_verified` result.

This contract covers the component-only S2-B boundary. It does not freeze Host
or Bridge writers, copy attachments, publish a backup, enable restore, or
authorize real-profile operations. Full D4-3 acceptance remains separate.

## Coordinator input and receipt

The coordinator accepts a previously validated `FinanceBridgeConfig`, an
explicit Application Support root and registered profile ID, finite snapshot
limits, and optional bounded gate-wait and hold durations. The limits are the
existing `DiskSnapshotLimits` subset: maximum Core database bytes, maximum
stage bytes, minimum free bytes, and pages per backup step. Invalid or
unbounded values fail before the worker opens SQLite.

The returned component receipt binds the cut ID, private stage, byte length,
SHA-256, page count, schema-object count, and `DELETE` journal mode. The
coordinator selects the profile and fixed `core.sqlite` output from trusted
configuration. Request JSON cannot supply a path, executable, descriptor
number, Python option, SQL, or arbitrary operation.

## Worker handoff

The coordinator acquires the profile's exclusive gate and reserves one worker
before spawning it. The fixed `delegated-cut-worker-v1` protocol uses these
descriptor roles:

| Descriptor | Role |
| --- | --- |
| FD 3 | One-use parent/child control channel and handshake |
| FD 4 | Inherited description of the coordinator's exclusive profile gate |
| FD 5 | Validated registered profile directory |
| FD 6 | Newly created private snapshot stage directory |

The worker validates the live handshake, exact request fields, profile
registration digest, installed artifact and schema identities, limits digest,
descriptor roles, fixed `core_snapshot` operation, and remaining deadline
before SQLite access. It uses the inherited exclusive gate and does not acquire,
upgrade, or unlock a profile gate itself. It opens the enrolled Core source,
stages `core.sqlite`, closes the source, revalidates the profile, and returns
bounded staged-output evidence. It cannot start the reader, invoke a provider,
run migrations, write financial facts, or spawn descendants.

Control frames are size-bounded and reject missing, extra, duplicated, or
malformed fields. The worker does not return a filesystem path as authority.
Device and inode identities cross JSON as canonical decimal strings so values
above JavaScript's exact-integer range remain unchanged; the reader validates
their syntax and native range before use.

## Close, reap, and independent readback

A success frame reports the worker's operation result; it does not prove that
the process has exited. The coordinator waits for actual worker close and reap
while retaining the exclusive gate and child reservation. Only then does it
start `finance_core.managed_snapshot_reader` in a fresh process. The reader
uses the fixed stage directory and staged identity evidence, opens SQLite in
read-only mode, checks integrity, foreign keys, schema readability, journal
mode, content hash, file role, and absence of SQLite sidecars, then closes
SQLite before returning its bounded result.

The coordinator waits for the reader to close and reap as well. Only a
successful independently verified result can become the component receipt.
The source connection is closed before readback starts, and the worker never
spawns the reader.

On timeout or cancellation, the coordinator uses bounded termination and reap
handling. A timeout or success frame alone does not release the reservation.
If child closure remains unknown, the call fails as unknown and retains the
exclusive gate and reservation while the child could still hold SQLite or
inherited descriptors. Failed or partial stage files remain for explicit
disposition; the component does not delete unknown files to make a retry work.

## Synthetic acceptance boundary

Tests use fresh temporary registered profiles and synthetic Core data. The
S2-B proof must exercise a real Node coordinator and Python child, exclusive
parent/worker operation without shared-gate reacquisition, shared-writer
contention, wrong/missing/replayed delegation, one-worker reservation,
success-frame-before-exit, timeout/cancellation, parent death, and uncertain
reap. It must also show that neither requests nor test data select paths or
cause provider calls.

The closed output must preserve committed WAL rows and rowid gaps, schema and
migration-ledger state, financial audit evidence, and synthetic correction
history, while the source remains unchanged. This component proof does not
establish attachment coverage, Host/Bridge consistency, complete-profile
publication, restore safety, or D4-3 completion. S2-B acceptance and C01–C10
remain unclaimed until their required observed tests and reviews complete.

See [the component disk-snapshot contract](managed_disk_snapshot_v1.md) for
stage and readback checks that apply inside this protocol.
