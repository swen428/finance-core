# Production instance write admission

`finance_core.instance_admission` is the public boundary for admitting and
opening one explicitly configured production profile. It binds durable control
state, the selected profile and the SQLite connection lifecycle before a
caller can perform a production write. It does not choose a profile or grant
permission from an ordinary request.

This API is intended for the supported Linux runtime. macOS may exercise the
portable state and locking behavior where the host supports it; that does not
create a runtime or platform bypass.

## Trusted composition and admission

Trusted composition creates a `TrustedInstanceConfig` with a private control
root, a separate fixed profile locator, profile and instance IDs, declared
layout, pinned configuration and Core identities, and an approval verifier.
The control root must be canonical, private, non-symlinked, and outside the
profile locator and data tree. The verifier is trusted application code; it receives the approval
reference, action, and binding hash and must validate durable approval evidence.

Operational calls provide references only. They cannot select a database path,
profile, verifier, key, human principal, module, or policy. A claimed approval,
a constructed data object, or a valid-looking reference alone is not proof.

Trusted composition may provide an immutable `ApplicationSchemaContract` with
a schema ID and an ordered tuple of DDL statements. The default is no
application extension, in which case only the installed Core schema is
accepted. A declared extension may add ordinary application-owned tables and
their own indexes and triggers, but cannot change Core schema objects, create
temporary or virtual tables, attach another database, run DML as an initializer,
or use a trigger to mutate Core tables. Foreign keys may target only tables
declared in the same application contract, including the declaring table
itself; they cannot point into Core, the migration ledger, staging
authorization, or an undeclared table. The contract is bounded to 256
statements and 256 KiB of DDL. Its schema ID and statements are part of trusted
composition; ordinary requests cannot supply or change them.

Preparation and admission are explicit operations. They accept only a complete,
new fixed profile whose database is absent or blank and whose profile identity
and installed migration resources match the expected values. The production
profile reuses the existing migration ledger 001–060 unchanged. It does not
reuse the staging-creation authorization, rewrite migration history, or adopt
an existing database. An interrupted transition can be resumed only for its
original operation and approval.

On open, the complete installed Core schema and the exact trusted application
extension are checked against independently built expectations. The full
migration ledger is sealed alongside that schema contract. A changed schema ID,
DDL statement, Core or extension object, ledger row, checksum, fingerprint, or
application time fails closed. Pending authority publication is resumed only
after the database schema and ledger still match the recorded contract. This
does not add a Core migration or alter the default Core-only profile.

## Durable authority state

The control root stores a bounded canonical state document, a fixed pending
transition journal and immutable transition history inside the state document.
State changes are published atomically and durably, with file and directory
synchronization. A caller must receive successful completion only after the
new state is durable.

Missing, pending, malformed, corrupt, or identity-mismatched authority state
fails closed: production owners and operations are not issued. Recovery may
finish publication for the exact recorded operation and approval; it cannot
silently adopt another profile or start a different transition. If publication
completed and the process stopped before returning, recovery without a pending
journal is an observation only: the exact operation, approval reference,
binding and originally recorded approval digest must match durable history, and
the current database and ledger must still validate. Recovery returns the
current record without publishing another transition or replaying the old
action; a previous admission cannot be revived after fencing.

If a process stops after creating the pending filename but before writing a
complete journal, the empty or malformed artifact is preserved and blocks use.
The system does not infer which operation should be reconstructed; the ordinary
exact-resume API refuses it, so trusted-owner intervention is required before
the instance can be used again.

Inspection and transition results expose the record fields `binding`,
`db_identity`, `ledger_sha256`, `state`, `admission`, and `maintenance`. The
state progresses from `PREPARED` to `ACTIVE`, or permanently to `FENCED`. A
separate maintenance hold blocks production operations while maintenance is
underway. The hold records the current admission and authority versions plus a
protected baseline of the complete logical database contents, including
declared application tables and SQLite sequence state, together with its
validated schema and sealed migration ledger. Fixed profile role identities,
ownership, permissions and manifest bytes are also protected. WAL placement and
checkpoint mechanics do not change the logical baseline. Auxiliary work,
backup and restore payloads and the service's ACK state require their own
consumer proofs. The Core compares its declared baseline again before a safe
abort, including pending-transition recovery; any change keeps the hold in
place. The trusted verifier must also confirm that no protected effects
occurred, no replacement was installed, and the original admission remains
valid. A hold does not prove that an entire service has stopped or acknowledged
shutdown. Fencing is permanent and is not reversed by maintenance abort or
forensic reading.

## Owner, operation, and connection lifetime

Only one process may hold the production owner lease for an instance. A write
or read operation retains the owner and the relevant authority and profile
leases for the complete SQLite connection lifetime. The ordered write path is:

1. exclusive process owner;
2. shared authority-state lease;
3. shared profile write gate;
4. SQLite connection and transaction lifetime.

The service owns commit and rollback for its business transaction. Leaving an
operation rolls back only work that remains uncommitted, then closes the actual
connection before releasing leases. If connection closure is uncertain, the
connection and leases remain held until process exit; admission must not claim
the connection was safely released. An owner cannot be explicitly closed while
its operation is still active. A forked child cannot close an inherited owner
to release the parent's lease.

Production writes require a connection issued by an active production write
operation. Production reads use a real SQLite read-only connection. A legitimate
fenced instance may be read for forensics, but a fenced or maintenance-held
instance cannot be written. The admission checks bind issued connections to
the current process, owner, profile witness, and authority version. Forked,
closed, stale, replaced, or caller-created connections are refused before
financial work. These checks do not sandbox hostile code already trusted in
the process and do not replace SQLite's own transaction rules.

The existing staging guard remains valid with its current behavior. Financial
services accept either their existing staging connection or a connection
issued through this production admission boundary. Legacy APIs that accept an
arbitrary path remain staging-only. Production authority is stored in the
separate fixed control root; it adds no SQLite ledger migration and reuses Core
migrations 001–060 unchanged. This change does not redesign SQLite's global
authorizer or alter financial rules, calculation authority, evidence,
confirmation, or migrations.

## Public operations

The package exposes explicit operations to prepare a production profile,
admit and inspect an instance, resume its exact pending authority transition,
acquire the process owner, and open scoped read or write operations. It also
exposes maintenance-hold entry/inspection/safe abort, permanent fencing, and
connection guards for financial read and write entry points. Callers retain
issued witnesses and use context managers for owner and operation lifetimes.

The Core boundary does not register a transport or runtime, call a provider,
change credentials, wire private service configuration, assert full service
quiescence, or prove a complete backup or restore. Those remain separate
integration and acceptance work.
