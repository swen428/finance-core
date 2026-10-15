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
includes the declared SQL definition of SQLite's internal `sqlite_sequence`
table. Ordinary AUTOINCREMENT sequence values remain mutable database data and
may advance normally; an unexpected change to the table definition fails
closed. This does not add a Core migration or alter the default Core-only
profile.

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

The service owns the business transaction. A production write operation
opens an idle connection; it does not start or own the service transaction.
The service must issue its existing explicit `BEGIN` (normally
`BEGIN IMMEDIATE`) before a statement that writes. DML and main-schema writes
are refused while the connection is idle, including ordinary DML and CTE
`... RETURNING` statements. Reads may run while idle. This preserves the
service's transaction boundary and prevents an implicit pending write from
starting outside it.

Every application-issued `execute()` is checked against the current issued
connection and authority before SQLite sees it. Each `executemany()` row is
normalized and receives a fresh full admission check immediately before it is
passed to SQLite; a row is refused if the service ended its explicit
transaction after an earlier row. The normal service-owned `commit()` and
`rollback()` calls remain available. Python `commit()`, context-manager
commit, SQL `COMMIT`/`END`, and an outer SQL `RELEASE` receive a full current
admission check before their commit-capable action. Rollback remains available
to abort work safely when write eligibility has been lost. Production
`executescript()` is refused. This is a narrow transaction-control boundary,
not a general SQL sandbox.

The connection stays at its default legacy transaction settings; changing
`isolation_level` or `autocommit` is refused, and even assigning the same
setting requires a current admission check. Bound parameters must use
supported built-in values and containers; custom adapters and callback-driven
parameter objects are refused so they cannot run Python code after admission
checking. The supported service flow retains connection `commit()`,
`rollback()`, and default context-manager behavior; those mechanisms do not
start a hidden transaction at operation open or permit an idle write.

Operation cleanup latches closing, performs native rollback while a transaction
is active, closes issued cursors, closes the actual connection, and only then
releases lifetime and authority leases. If rollback or close is uncertain, the
connection, cursors, owner and leases remain retained; the operation must not
claim safe release. Retention protects ownership and blocks a competing owner
or maintenance drain. The explicit-transaction rule and rollback-before-
finalize cleanup provide the write-safety boundary; ordinary shutdown is not
relied on to cancel an implicit pending write. An owner cannot be explicitly
closed while its operation is opening, active or cleaning up. A forked child
cannot close an inherited owner to release the parent's lease.

Production writes require a connection issued by an active production write
operation. Production reads use a real SQLite read-only connection. A legitimate
fenced instance may be read for forensics, but a fenced or maintenance-held
instance cannot be written. The admission checks bind issued connections to
the current process, owner, profile witness, and authority version. Forked,
closed, stale, replaced, or caller-created connections are refused before
financial work. These checks do not sandbox hostile code already trusted in
the process and do not replace SQLite's own transaction rules.

## Production SQL authorization

Each factory-issued production connection uses a fixed private SQLite
authorizer and `cached_statements=0`. Trusted connection configuration and
the initial schema and migration-ledger validation happen before the authorizer
is installed; installation completes before the connection is published to an
application caller. No application SQL runs in that interval. If installation
fails, the connection is not issued. The public connection API cannot replace
or suspend this authorizer.

The authorizer evaluates SQLite's actual compile-time actions rather than
guessing from SQL text. It allows `SQLITE_READ`, `SQLITE_SELECT`, and
`SQLITE_RECURSIVE`. `INSERT`, `UPDATE`, `DELETE`, and main-schema DDL actions
are allowed only on a writable issued connection while SQLite reports an
explicit transaction is active. This denies all idle writes, including CTE
DML, without a keyword or substring heuristic. TEMP objects, virtual tables,
attachments, and detachments are denied. Unknown actions and unsupported
control verbs are denied; the callback does not use `SQLITE_IGNORE` to rewrite
a query.

The service remains the transaction owner. Its guarded `BEGIN` is allowed;
commit-capable Python and SQL entry points receive their full current
admission check before SQLite executes `COMMIT` or `END`, or an outer
`RELEASE`. Native rollback is allowed as a safe abort, including cleanup after
eligibility is lost. Savepoint `BEGIN`, `RELEASE`, and `ROLLBACK` are the
supported verbs; an outer `RELEASE` gets the same full admission check as
other commit-capable paths. Unknown verbs are denied. The authorizer itself
performs a fixed action decision only: it does not issue SQL, consult
filesystem or authority state, invoke caller callbacks, or recursively perform
admission.

`PRAGMA` authorization is finite:

| PRAGMA | Caller-available form |
| --- | --- |
| `database_list`, `foreign_keys`, `busy_timeout` | Read only, with no argument. |
| `table_info`, `table_xinfo`, `index_list`, `index_info`, `index_xinfo`, `foreign_key_list`, `foreign_key_check` | Read-only metadata query with either no argument (`None`) or a plain `str` object name. The string is used only as the metadata target. |
| `defer_foreign_keys` | Getter is allowed. Setter is limited to `ON` or `1` on a writable connection already inside its explicit transaction. |
| Any other PRAGMA or form | Denied. |

Trusted setup retains its fixed `busy_timeout` and `foreign_keys=ON`
configuration before authorizer installation. The `SQLITE_FUNCTION` action
uses this fixed native-function allowlist: `char`, `coalesce`, `count`,
`current_timestamp`, `glob`, `instr`, `json`, `json_array_length`,
`json_extract`, `json_type`, `json_valid`, `length`, `like`, `lower`, `ltrim`,
`max`, `min`, `printf`, `substr`, `sum`, `trim`, `typeof`, `upper`, and
`zeroblob`. These names cover the installed Core schema, approved business
queries, and fixed admission queries. The allowlist is an implementation
compatibility boundary, not a financial rule and not a general SQL or Python
sandbox. Caller-defined functions, extension loaders, and unlisted or unknown
function actions are denied. The PRAGMA branches and function set are
implemented in [`sessions.py`](../../finance_core/instance_admission/sessions.py)
and must remain aligned with this contract.

The existing staging guard remains valid with its current behavior. Financial
services accept either their existing staging connection or a connection
issued through this production admission boundary. Legacy APIs that accept an
arbitrary path remain staging-only. Production authority is stored in the
separate fixed control root; it adds no SQLite ledger migration and reuses Core
migrations 001–060 unchanged. Its fixed authorizer is limited to
factory-issued production connections and does not change
`human_drafts.set_authorizer` or any other staging/D1 authorizer. Financial
rules, calculation authority, evidence, and confirmation remain unchanged.

## Issued SQLite capability boundary

An issued production session supports a finite subset of Python 3.12's
`sqlite3` connection and cursor APIs. This is not a claim of full SQLite API
compatibility and does not sandbox arbitrary Python already trusted in the
process.

| Handle or capability | Production-session contract |
| --- | --- |
| Connection statements | Each `execute()` receives a full current-session check. Every `executemany()` row is normalized and receives a fresh full admission check immediately before native binding/step; it is refused if the explicit write transaction ended. The SQL authorizer also checks SQLite's actual compile-time action. `executescript()` is refused. |
| Connection transactions | The service explicitly starts its write transaction; admission does not start one. `commit()` and commit-capable context-manager/SQL paths require a full current admission check. `rollback()` remains available to abort safely. The default legacy context-manager behavior remains supported. `autocommit` and `isolation_level` stay at their configured legacy values; changing either requires a current admission check and a different value is refused. |
| Connection and cursor lifetime | The operation strongly owns each issued cursor until its checked native `close()`. Dropping a caller reference cannot finalize a live cursor behind the owner. Cleanup rolls back before cursor finalization, closes issued cursors, and closes the actual connection before releasing leases. A caller-supplied cursor factory cannot replace the session cursor. |
| Cursor statements and results | Cursor `execute()` and each `executemany()` item are checked. `fetchone()`, `fetchmany()`, `fetchall()`, iteration, and cursor reuse perform a full current-session check before native result consumption, conversion, or lookahead. `fetchmany(size)` accepts only `None` or an exact built-in `int`. Cursor `row_factory` accepts only the actual `None` or `sqlite3.Row` objects. `setinputsizes()` and `setoutputsize()` remain guarded no-op size hints. |
| Row and text conversion | Before each result operation, the connection must still use the actual `sqlite3.Row` and `str` factories and the cursor row factory must be allowed. These checks precede conversion and native lookahead. Custom row factories and adapters cannot execute as part of a production statement. |
| Simple diagnostics | `getconfig()`, `getlimit()`, and `interrupt()` require a valid issued session. Native status and metadata attributes such as `in_transaction`, `total_changes`, and cursor metadata are observations only; they do not authorize work or promise session liveness. |
| Database transfer and serialization | `backup()`, `serialize()`, `deserialize()`, and `iterdump()` are refused. Production does not expose SQLite's incremental blob handle: `blobopen()` is refused for both writable and read-only sessions. Native backup and restore APIs are outside this work package. |
| Extension and callback registration | `enable_load_extension()`, `load_extension()`, `create_function()`, `create_aggregate()`, `create_window_function()`, `create_collation()`, `set_authorizer()`, `set_progress_handler()`, and `set_trace_callback()` are refused on an issued production session. |
| SQLite runtime changes | `setconfig()` and `setlimit()` are refused. |

`Connection.__call__` is recorded as an internal compiler entry in the Python
3.12 capability inventory: the standard library may use it while compiling a
statement from guarded `execute()`. Calling it directly is outside the
supported application API, and this boundary does not claim that it is
technically blocked. The supported application entry remains the guarded
`execute()` or `executemany()` path. No general SQL parser or alternate callback
sandbox is part of this boundary.

On successful cleanup, the actual-close and lease-release guarantee covers a
supported operation's connection and issued cursors, including a partially
consumed issued cursor.
Direct access to `Connection.__call__`, a returned native statement, base-class
methods, a native cursor, or connection reinitialization is outside that
contract. Such unsupported handles have no actual-close or descriptor-release
promise from instance admission.

These limits apply to connections issued to application callers. The trusted
admission path may use internal SQLite operations before it publishes a
session. Legacy `human_drafts.set_authorizer` remains staging/D1-only; it is
not called on an issued production session. Monetary calculation authority,
approval verification, maintenance holds, durable history, and operating-system
privacy checks keep their existing contracts.

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
