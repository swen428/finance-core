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
