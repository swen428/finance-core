# Bridge owner-state export (v1)

## Scope

The Bridge exporter freezes the Bridge-owned handoff tree for one existing
Finance profile. It is one component of a complete D4 consistency cut, not a
standalone backup or restore product. A manifest for this stage identifies the
bytes that were staged; it does not establish that SQLite databases and every
other D4-owned component describe the same complete state.

The exporter does not create or initialize profiles, run database backup or
restore, finalize financial facts, reclaim handoff files, change the selected
profile, or activate a restored copy. Profile creation, database cut logic,
full D4 acceptance, stage promotion, restore authorization, and runtime
activation remain separate boundaries.

## Entry point and cut session

The public entry point is:

```ts
exportBridgeOwnerState(
  { applicationSupportRoot, profileId, runtimeRoot },
  { waitMs?, maxHoldMs? },
)
```

The application-support root and runtime root must be explicit canonical
paths, and the profile ID must select the profile already bound by
`profile.json`. The exporter validates the fixed profile path boundary and
requires the existing fixed profile gate. It does not read or require
`FINANCE_RUNTIME_ROOT`; that environment setting remains part of the Core
Python profile/runtime boundary. Bridge does not infer a profile from the
checkout, current working directory, message, or environment.

The profile owner must initialize the empty `workspace/handoff/` directory and
its fixed owner lock before the first backup, including profiles that have not
received a photo. This exporter does not create either entry during a cut: a
missing entry fails closed. The private profile/runtime wiring must fulfill
this precondition before a blank profile can be backed up; the current private
profile adapter does not yet provision it.

The full owner-state read, validation, hash calculation, and stage write run
within one Bridge exclusive profile-gate lease in one Node process and one cut
session. The exporter checks the cooperative hold bound at each transition.
It does not release and reacquire the lease between discovering source files,
copying them, and producing the manifest. It releases the lease only after
protected cleanup has completed.

## Frozen handoff and stage result

`HandoffPublisher.exportFrozen(context, sink)` freezes the recognized handoff
records, payloads, reclaim intents, and pending publication residue under the
handoff publication lock. Each staged file is represented with its role,
frozen name, byte size, and SHA-256. Slot metadata records its canonical key
hash, attachment hash, type, and custody state. The export binds that frozen
description to the fixed profile ID and one cut ID.

On success, `exportBridgeOwnerState` returns the frozen handoff description, a
manifest entry (`relativeName`, `byteSize`, and `sha256`) for the staged export,
and `stagePath`. The stage is private and remains a reviewable intermediate
artifact. The caller must validate it together with the other complete D4 cut
components before any separate promotion decision. Merely receiving a
manifest or a path is not full-cut acceptance.

## Incomplete and invalid state

Incomplete durable state is evidence and must remain recoverable. A recognized
standalone `.finance-bridge.record.pending` is copied unchanged and marked
`incomplete`; reclaim intents and their associated handoff entries are
preserved for later Core custody verification. The exporter does not clean,
retry, reclaim, or repair these entries. Their presence means that the Bridge
handoff portion is not complete, even when the staged bytes and their manifest
hashes verify.

A final handoff record must have its matching final image. If the image is
missing, the export fails unless a matching, already sealed reclaim intent
explains the state. A valid `.finance-bridge.payload.pending` does not permit
the owner export to freeze that record as complete or incomplete; D3 must first
finish its recovery/replay. Unknown directory entries, malformed records, bad
or mismatched hashes, unsafe identities, conflicting pending residue, and
unexplained missing images fail closed; unknown files are never silently
omitted from the frozen inventory.

If an operation fails after creating a stage, it preserves that stage for
owner inspection and rejects without returning the success shape. A stage is
never automatically renamed, published as a completed backup, or marked
complete by the Bridge exporter. Cleanup or promotion requires a later,
explicitly authorized owner operation.

## Synthetic acceptance

Tests build a complete synthetic profile under a temporary directory, create a
valid `profile.json`, explicitly initialize its fixed gate, and populate only
synthetic handoff files. Success cases cover a retained image, standalone
pending record, and reclaim intent. They verify the frozen contract, profile
and cut identity, per-file hashes, manifest size/hash, and stage permissions.
Failure cases cover a final record with no final image (including a valid
pending payload), unknown files, malformed or tampered record/payload hashes,
and preserved stage/source evidence. Tests remove only their own temporary
synthetic tree.

The exporter and these tests do not activate a real profile, open the live
database, use real owner state, call an external provider, or supply complete
D4 backup acceptance.
