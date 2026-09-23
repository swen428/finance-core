# Local controlled correction entry (source candidate)

This describes the generic Core local entry point. It operates only on an explicitly authorized staging database. Installing this source does not publish a package, upgrade a consumer, or activate a live correction workflow.

Set `FINANCE_RUNTIME_ROOT` to an existing canonical owner-controlled directory with a `database` directory. The staging database must already have migration 051, must pass the existing staging guard, and must be a canonical owner-owned regular file with mode `0600` and one hard link. The operator supplies its exact path only during first setup:

```sh
python -m finance_core.correction_adapters.cli provision \
  --database /absolute/canonical/staging/ledger.sqlite \
  --actor ORIGINAL_AUTHENTICATED_ACTOR
```

Provision creates one owner-only policy and binds the current UID, original actor, key, realm and database inode. It refuses an existing policy or any correction records. Normal commands obtain the database and actor from this policy; they have no database, actor, key, realm or verifier options.

Normal commands open one registered connection through the trusted local factory. The factory retains a no-follow file descriptor, compares its inode and the canonical path around connection opening, and rechecks both on current reads and before a correction commit. Python's standard `sqlite3` API does not expose its internal database descriptor, so this does not claim protection against a malicious same-UID process that replaces and restores the path between checks; that race is outside the local host threat model.

```sh
python -m finance_core.correction_adapters.cli show TRANSACTION_ID
python -m finance_core.correction_adapters.cli preview TRANSACTION_ID \
  --reason 'Correct the entered total' --amount 13.50
python -m finance_core.correction_adapters.cli confirm PLAN_ID
```

`show` verifies original authority and the full effective history. `preview` appends an expiring plan only. `confirm` first checks for a committed result from a lost reply; for a new decision it requires both input and output terminals, displays all before and after fields and the full reason, and asks for the exact plan ID plus a fresh challenge. The terminal never prints the signing key or signed proof. An old plan can be recovered by its ID, but a stale uncommitted plan needs a new preview.

If first provision was interrupted and left a malformed policy, retain it as evidence. The owner may call `finance_core.correction_adapters.policy.quarantine_incomplete_policy(Path(...))` only after checking the exact canonical staging path. That function opens the database through its normal guard, takes a write lock, proves all five correction tables empty, and moves the malformed owner-only policy to a unique retained path. It refuses a valid policy or any correction record. Run ordinary `provision` afterward. There is no automatic overwrite, key reset or reattachment. A copied, renamed, restored or replaced database remains refused; backup and device restore authority is a later work package.
