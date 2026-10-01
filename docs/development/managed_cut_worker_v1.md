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


## Committed Core snapshot bundle

`runManagedCoreSnapshotBundle()` adds a separate, bounded whole-package entry.
Its successful receipt is `core-snapshot-bundle-receipt-v1`, with
`scope=core_committed_snapshot` and `status=snapshot_verified`. The existing
`runManagedCoreSnapshot()` remains a database component entry with its original
receipt. Neither result proves encrypted/cloud backup, full profile custody,
restore, or authorization for a restored instance.

The bundle contains all already committed Core rows, including pending intake,
source evidence, decision/authorization, calculation snapshots, audit and
correction history. Host input not yet persisted in Core, uncommitted Bridge
handoffs and external delivery outcomes are outside this scope. No rows or
historical hashes are rewritten, and no financial or recovery authority is
created. Current supported writers must use the existing profile gate;
unsupported commands retain their pre-effect refusal.

### One exclusive lifetime

The coordinator holds the same EX gate through online SQLite backup, actual
source/output closure, attachment copying, worker close/reap, a fresh fixed
reader's independent whole-package validation, reader close/reap and final
member/manifest identity checks. It releases EX only after that lifecycle is
resolved. A staged/verified frame or process `exit` alone is insufficient.
Unknown closure retains the existing uncertain-cut isolation; late closure can
release a failed attempt but cannot promote it to success. Failed or partial
stages are retained. The component entry must not be followed by attachment
copying after its EX has already been released.

The added `delegated-cut-bundle-v1` protocol has only the fixed
`core_bundle_snapshot` and `core_bundle_readback` operations. It preserves the
same FD3 control, FD4 gate, FD5 profile and FD6 stage roles, one-use handshake
and 8192-byte maximum frames. No request selects an arbitrary command, module,
SQL query, attachment root or output path. Inventories stay in the manifest;
control messages carry only bounded evidence summaries.

### Fixed layout and content authority

A newly created private stage uses this layout:

```text
core-cut-<cut-id>/
  db/core.sqlite
  attachments/<sha256-prefix>/<sha256>.jpg|png|pdf
  manifest.json
```

Directories are owned private `0700`; database and manifest are single-link
`0600` files; copied attachments are single-link `0400` files. Database staging
and readback reuse the original component checks inside `db/`; the old flat
component whitelist is not broadened. Source attachment access is anchored to
the registered workspace's attachment root and fixed shard/basename, with
symlink and role checks. The manifest is written and synchronized last.

`core-attachment-reference-registry-v1` reads a fixed set of current-schema
fields from the closed snapshot. All `attachments` declarations are included,
including unused/pending declarations. The registry also reconciles explicit
attachment FKs, intake evidence, payment images, existing OCR byte evidence,
statement/PDF source files, structured evidence and registered file references
in audit/calculation/correction records. It does not search arbitrary user
text or treat every JSON string as a file path.

Expected byte hashes come from existing, explicitly associated source evidence.
A nullable `attachments.file_hash` can be validated using another existing
byte hash for the same attachment; a filename digest or a newly computed hash
alone cannot create authority. Non-null hashes and expected lengths must agree.
Path-only references require a supported canonical managed location and existing
byte evidence. Explicitly linked historical source labels are retained in the
DB rather than opened as arbitrary external paths. Missing, ambiguous,
unsupported or conflicting references fail the package; they are never omitted
to produce a successful receipt. No migration, OCR invocation or historical
repair is performed by this entry.

### Manifest and independent verification

`core-committed-snapshot-manifest-v1` is canonical UTF-8 JSON with exact fields,
unique keys and deterministic member/reference ordering. It binds the scope,
registry and fixed-budget versions, cut ID, verified Core artifact/API/schema
identity, database metadata and complete migration-ledger digest, member paths,
lengths and SHA-256 hashes, plus reference counts/digest.

The snapshot point is `exclusive_core_commit_state`: its time is recorded after
safe source opening/recovery and before online backup while EX excludes the
supported writers. The cut ID and closed DB hash bind the actual content; the
time is descriptive and is not a fabricated SQLite commit identifier. Content
completion and later independent verification times are separate. The manifest
lists database/attachment members but not itself; its own hash and length are
bound by the receipt.

The fresh reader derives the full reference/hash set again from the closed DB.
It compares DB-derived members, manifest entries and actual package files in
both directions, as well as hashes, lengths, schema/ledger, integrity and
foreign keys. It cannot trust only the worker's list. A worker that omits an
image from both files and manifest must still be rejected. Extra or duplicate
members, unsafe paths, unknown versions and partial output also fail.
After the reader really closes, the coordinator checks manifest content and
bounded tree identities and rehashes every member under EX before creating the
final receipt. These final checks share the same deadline as the backup.

### Fixed resource boundary and synthetic evidence

`core-snapshot-bundle-limits-v1` fixes DB size to 64 MiB, DB staging peak to
128 MiB, each attachment to 20 MiB, unique attachments to 4096, references to
65536, manifest to 1 MiB, total stage files to 256 MiB, minimum free reserve to
64 MiB and online-backup batches to 256 pages. EX has the existing 30000 ms
maximum; callers can shorten wait/hold durations, not enlarge this capacity.
Backup, enumeration, copying, synchronization, readback and final checks share
the deadline. Reaching the size limits does not guarantee completion within
that time. Exhaustion fails; no truncation can become a complete package.

Synthetic tests exercise the real coordinator and fixed children, committed
WAL/pending data and images, duplicate references, existing-hash alternatives,
missing/wrong/omitted members, unsafe paths, writer competition and child
closure/cancellation. An independent consumer acceptance must use the same
exact candidate artifact and compare the closed package with a frozen expected
inventory, including an already committed corrected event's original/effective
amounts, source image and linked authorization/calculation/audit history.
This is retained-record equivalence, not restore or new write authorization.
Component tests, this contract and a CI job in progress are not whole-package
acceptance evidence.
