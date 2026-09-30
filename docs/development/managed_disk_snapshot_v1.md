# Managed disk snapshot primitive (D4-3 P2)

`finance_core.managed_disk_snapshot` is an internal, component-level SQLite
backup primitive. A trusted caller supplies an already
opened and recovered source connection, a newly created private `0700` stage,
finite limits, and a monotonic deadline. The caller owns source connection
closure and the exclusion of concurrent writers. A path or connection alone
does not prove those conditions. This module does not acquire a profile gate,
publish a backup, or return a complete-cut verification result.

The primitive creates only `core.sqlite` in the empty stage, refusing an
existing output. Before writing sensitive bytes, it rejects extended ACL allow
grants on the stage and newly created file even when their mode bits appear
private; the file must be owner-held with one link. It uses SQLite's online
backup API in bounded page steps.
After backup, it closes the destination, reopens that private file, checkpoints
WAL when present, and requires the actual journal mode to become `DELETE`.
It never removes a SQLite sidecar by hand. S2-A separates writing the closed
file from independently reading it back. The writing step checks its identity,
size, hash and sidecars, then returns only an **unverified stage descriptor**.
That descriptor is not a backup success receipt. The caller remains responsible
for closing the source and ending the writing process before independent
readback. Readback uses a fresh Python process with ordinary read-only SQLite
access to check journal mode, database integrity, foreign keys, and schema
readability. It then checks the closed-file hash, sidecars and private-file
role again and synchronizes the file and stage directory. Only after all of
that may it return a component receipt with the fixed output path, size,
hash, page count and schema count. The existing `create_disk_snapshot()` entry
keeps its verified component behavior by composing both steps; it is not the
entry for a worker that must exit before readback.

Any collision, limit or deadline breach, SQLite error, incomplete checkpoint,
unexpected sidecar, readback mismatch, or sync failure returns no receipt.
Failed stages and unexpected source files remain for explicit disposition.
Synthetic acceptance tests cover committed WAL content, rowid gaps,
fresh-process readback, collision, permissions, and deadline refusal. Lawful
hot-journal recovery belongs to the earlier managed-open component; this
primitive accepts an already recovered connection. These tests do not
establish source profile identity, cross-component consistency, power-loss
durability, financial recovery, publication, encryption, or cloud delivery.
Those claims require the later managed owner and complete-cut integration.
