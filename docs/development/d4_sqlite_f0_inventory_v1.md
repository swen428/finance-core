# D4 SQLite upstream call-site inventory diagnostic

This diagnostic records which Unix VFS calls and upper SQLite `sqlite3Os*`
calls are present in one preprocessed upstream SQLite build. It is a **static
candidate list**, not a reachability proof, native file-identity guard, backup,
restore, or F0/G1 pass. It contains no Finance database or user data.

`d4_sqlite_f0_probe.py` accepts the pinned SQLite 3.53.1 amalgamation ZIP,
checks its ZIP and `sqlite3.c` SHA-256 values, preprocesses the source with a
fixed set of diagnostic flags, obtains a Clang JSON AST, and runs
`d4_sqlite_callsite_inventory.py`. The latter records Unix VFS calls with both
preprocessed and original source lines, the original/preprocessed source
hashes, the `aSyscall` slot table, and upper SQLite `sqlite3Os*` call sites.
Direct calls whose effects have not been classified remain explicitly
`unclassified_direct_call`; indirect method/function-pointer calls remain
`indirect_dispatch_candidate`. Neither category is safe by default. The probe
records the host, compiler, Python, libc and output-directory filesystem. Its
large AST is discarded after hashing;
the preprocessed source, inventory and probe metadata remain available for
inspection.

On a non-production branch, the manual `validate` workflow input
`d4_sqlite_f0_probe=true` enables a conditional diagnostic step inside the
existing Ubuntu `quality` job. PR and push runs keep the same job inventory
and required validation behavior. A failed manual diagnostic fails that run's
`quality` job. The artifact contains the exact checkout identity even if the
probe fails after starting. A green diagnostic step means the inventory was
collected, **not** that its calls are safe or that D4-3 is complete.

The remaining proof must connect every supported entry to actual native file
effects and classify each reachable branch as admitted FD use, controlled
namespace use, or refusal before effect. It must close function-pointer and
dynamic VFS/system-call dispatch, failure cleanup, owner and sidecar state,
and non-SQLite I/O. A later native patch and each platform need their own
preprocessed inventory, independent runtime observation, legitimate hot/WAL
recovery, and wrong-object tests. No Mac result substitutes for Linux, and no
upstream result substitutes for a patched candidate.
