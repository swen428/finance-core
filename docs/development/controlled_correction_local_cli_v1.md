# Local controlled correction entry (source candidate)

This describes the generic Core local entry point. It operates only on an explicitly authorized staging database. Installing this source does not publish a package, upgrade a consumer, or activate a live correction workflow.

Set `FINANCE_RUNTIME_ROOT` to an existing canonical owner-controlled directory with a `database` directory. The staging database must already have migration 051, must pass the existing staging guard, and must be a canonical owner-owned regular file with mode `0600` and one hard link. The operator supplies its exact path only during first setup:

```sh
python -m finance_core.correction_adapters.cli provision \
  --database /absolute/canonical/staging/ledger.sqlite \
  --actor ORIGINAL_AUTHENTICATED_ACTOR
```

Provision creates one owner-only policy and binds the current UID, original actor, key, realm and database inode. It refuses an existing policy or any correction records. Normal commands obtain the database and actor from this policy; they have no database, actor, key, realm or verifier options.

Normal commands obtain registered connections through the trusted local factory. For ordinary staging, the factory retains a no-follow file descriptor, compares its inode and the canonical path around connection opening, and rechecks both on current reads and before a correction commit. Python's standard `sqlite3` API does not expose its internal database descriptor, so this does not claim protection against a malicious same-UID process that replaces and restores the path between checks; that race is outside the local host threat model.

```sh
python -m finance_core.correction_adapters.cli show TRANSACTION_ID
python -m finance_core.correction_adapters.cli preview TRANSACTION_ID \
  --reason 'Correct the entered total' --amount 13.50
python -m finance_core.correction_adapters.cli confirm PLAN_ID
```

`show` verifies original authority and the full effective history. `preview` appends an expiring plan only. `confirm` first checks for a committed result from a lost reply; for a new decision it requires both input and output terminals, displays all before and after fields and the full reason, and asks for the exact plan ID plus a fresh challenge. The terminal never prints the signing key or signed proof. An old plan can be recovered by its ID, but a stale uncommitted plan needs a new preview.

If first provision was interrupted and left a malformed policy, retain it as evidence. The owner may call `finance_core.correction_adapters.policy.quarantine_incomplete_policy(Path(...))` only after checking the exact canonical staging path. That function opens the database through its normal guard, takes a write lock, proves all five correction tables empty, and moves the malformed owner-only policy to a unique retained path. It refuses a valid policy or any correction record. Run ordinary `provision` afterward. There is no automatic overwrite, key reset or reattachment. A copied, renamed, restored or replaced database remains refused; backup and device restore authority is a later work package.

## Managed synthetic profiles

The trusted local factory also accepts the exact `database/staging.sqlite` of an already enrolled managed synthetic profile. First provision requires the configured runtime root to match that profile, migration 051 and all five correction tables to be empty. Existing original transactions may already be present; the empty-ledger requirement concerns correction authority/history, not the financial ledger. Provision neither enrolls an arbitrary directory nor migrates or enables a real database.

The managed factory obtains a short managed database session and privately registers its connection. The policy retains the original actor, signing key, realm, instance, canonical database path and file identity. Reads and commits recheck the same policy and verified profile identity. While SQLite is live these checks use the enrolled profile metadata and controlled session witness; they do not open and close a second descriptor for the main database. Full file checks run before SQLite opens and after it really closes, under the existing process lifetime exclusion. A managed validation failure is terminal and never falls back to the ordinary staging opener. Caller-created connections cannot register themselves or select another verifier.

A new `confirm` has three phases: read/recover the exact saved plan in a short session; close that session and release the profile gate before displaying the original terminal challenge; then open a fresh trusted session and apply the signed plan using the existing checks. The latter phase rechecks the persisted plan, policy, actor and instance, expiry, source, predecessor, version and receipt calculation. A change while the operator is thinking requires a new valid preview/decision rather than silently adopting newer values. A previously committed plan is recovered without asking for another signature or creating another economic event. `show` and `preview` each use one short session.

This source slice proves synthetic local correction and the independent authenticated delivery-receipt CLI. It does not add correction commands to the Bridge dispatcher, enable installed Host delivery, grant arbitrary database access or complete the backup acceptance suite. Interrupted or rejected first provision retains any policy bytes as evidence; no automatic overwrite, key reset or managed recovery bypass is introduced. The existing manual incomplete-policy quarantine remains an ordinary staging operation; managed incomplete-policy recovery is not enabled by this slice.
