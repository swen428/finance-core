# Prepared encrypted envelope v1

This opt-in API seals exact protected preparation content in a private local
capsule and exposes an authenticated encrypted public envelope. Independent
checking returns only `CONTENT_VERIFIED_OFFLINE`, `PREPARED_PROJECTION` and
`source_finalization="NOT_ESTABLISHED"`. The projected append does not prove
that the source applied it. No API here posts, confirms, restores, promotes,
admits a writer or supplies online write authority.

## Protected content and trusted roots

The actual `PreparedFinalizationServiceV1` gains
`export_content_v1(operation_id, expected_intent_sha256=...)`. One protected
read revalidates installed/source identity, the complete journal, binding,
archive authentication and current native evidence. It returns original
canonical intent bytes and ordered package BLOBs after actual rollback/close;
it does not compose separate inspection/material snapshots.

`PreparedContentAnchorV1` binds source identity, operation, intent digest,
projected frontier and every ordered package digest/length. The export's
observed local state is outside that immutable anchor. First sealing requires
a current pending preparation. An already sealed operation remains checkable
after application, cancellation or review expiry, without renewing eligibility
to apply. The detached checker uses the authenticated v2 closure and original
062 journal verifier against a trusted, private in-memory before-prefix; it
destroys that database before returning. Historical v1/061, v2/062 and the old
D4 crypto contracts retain their separate wire formats and limits.

Supply `NativeAgeToolchainV1` with explicit canonical absolute paths to `age`
and `age-keygen`, installed in a controlled directory. Only packaged
`resources/crypto/age_toolchain_v1.json` identities are admitted: age v1.3.2,
`darwin-arm64` and `linux-x86_64`. Runtime does no download, shell or PATH
lookup and accepts no caller-selected trusted hash. Each invocation checks
exact hashes/sizes/version, tool/file/ancestor custody and stable identity.
Unknown platforms fail closed.

The caller supplies recovery **public** recipient R, a positive key epoch,
independent key IDs and a separate exact 32-byte envelope HMAC root. Native
evidence authentication remains independent. Sealing generates a fresh X25519
temporary identity T with the pinned keygen and encrypts to exactly R and T.
The key roles are not interchangeable. R private material never enters the
writer or capsule. Identities use bounded anonymous native channels, never
command arguments, environment variables or standalone plaintext key files.
No plaintext frame is persisted. Full native exit zero, authentication,
content validation and actual resource close precede any successful result.

## Local publication and public bytes

Import `PreparedEnvelopeStoreV1` and `PreparedEnvelopeLimitsV1` from
`finance_core.lifecycle_v2.prepared_envelope_v1`. Construct with an existing
canonical owner-private 0700 root, trusted namespace, recovery key ID and
recipient, epoch, HMAC ID/root, native evidence authenticator and toolchain.
Managed directories are 0700, files 0600. Trusted ancestors follow the existing
profile rule, including root-owned sticky OS temporary ancestors; symlink
spellings of the configured root are refused.

`store.seal(actual_preparation_service, operation_id,
expected_intent_sha256=...)` calls the actual protected export. A DTO, caller
database path or overridden source substitute cannot supply this proof. Retain
the returned `PreparedEnvelopeRefV1` independently before source loss. It binds
trusted key context, opaque context ID, content anchor, temporary recipient,
exact envelope and ciphertext digest/length. Never derive an expected ref from
the object being checked.

One namespace/operation slot excludes intent, epoch and generation from its
name, preventing these changes from creating a second slot for that operation.
The private `FCLOC001` capsule retains T's **raw private identity** and exact
public envelope under a separate authenticated private header. It is local
decrypt capability, not an encrypted-key or erasure guarantee. Never upload,
scan or export capsule files as a backup. `store.read(external_ref)` fully
validates/decrypts with T and returns only exact public `FCENV001` bytes.

Namespace/operation locks cover quotas and publication. The store writes an
exclusive candidate, fsyncs it, validates same-descriptor readback, publishes
with a no-replace hard link, removes only the controlled candidate link, syncs
file/parent and rechecks names/inodes. Only the exact two-name, same-inode
publication window is accepted. All data/directory/lock descriptors actually
close before success. Complete persisted candidates resume with their original
bytes; retries do not generate new T identities or ciphertext. No automatic
garbage collection, overwrite or deletion of unknown residue is provided.

Partial/corrupt files, unprovable durability, unexpected links/paths, lost
publication replies and ambiguous close withhold success. `ARTIFACT_UNKNOWN`
means local artifact uncertainty, not a Core database commit outcome. Ambiguous
close poisons that store object and retains known custody; it never retries an
FD integer the OS may already have reused. After process exit, a fresh process
can independently verify complete persisted state and finish sync/close.
Incomplete candidates remain evidence and block their slot; they are not
absence or permission to re-encrypt. Existing B1 pending/write gates remain.

## Independent checking and limits

`check_prepared_envelope_v1` takes public bytes, an independently saved expected
ref, R identity, external HMAC/evidence roots and qualified tools. It needs no
source owner/database/control/evidence port, capsule or T. It checks exact
digest/length and canonical manifest/HMAC, waits for full native R decryption
and exit zero, validates the complete `FCPREP01` frame/external anchor and then
authenticates native evidence and original intent bindings. Wrong keys,
role/context mismatch, damaged tails, duplicates, unknown fields, floats,
trailing bytes and re-MACed invalid inner content refuse a result. A valid
envelope HMAC does not replace original financial-evidence authentication.

Defaults: intent/package 16 MiB each; chain 64 MiB/32 packages; metadata 64 KiB;
manifest 16 KiB; private header 4 KiB; identity 256 bytes; namespace 16 slots
and 512 MiB of distinct staging/final capsule bytes. Exact positive integer
limits can only be lowered. Native closure limits independently remain in
force. Child deadlines default to 30 seconds with a hard 120-second ceiling.
Derived wire maxima are not measured memory or storage-capacity guarantees.

Acceptance includes real two-recipient crypto, a valid frame above the old
8 MiB boundary, source-free fresh-process checking, actual killpoints,
syscall/close/path/quota/concurrency faults, exact wheel and separately
installed sdist execution outside the checkout. Final Hosted Linux executes
the pinned Linux tools and installed closures before merge without skips;
see [release validation](release_validation_v1.md).

In installed-probe receipts, checkout visibility means Python import
visibility: isolated children import Core only from the selected installation.
The code checkout itself is not removed or placed behind an OS sandbox. The
synthetic source ledger/control and capsule directories are actually removed
before the fresh recovery reader starts; recovery keys arrive through an
anonymous stdin pipe, independently of the public object and saved reference.
Secret-bearing phase results use a separate anonymous response pipe. The
parent passes only its numeric file descriptor with `--response-fd`; key or
result contents are not passed in arguments or environment variables, and
keys or secret-bearing results are not written to separate result files.
Before reading the phase configuration or stdin, activating the installed
runtime, or running native key generation, the child requires this descriptor
to be a writable FIFO above descriptors 0–2 and not an inode alias of a
standard stream. It writes a JSON object under the 128 KiB response bound and
closes the descriptor once. The parent drains the pipe while the child runs
under its deadline and accepts a result only after successful child exit, an
empty stdout and stderr, and a complete valid response. Missing, malformed,
empty or oversized responses, unexpected diagnostic output, timeout and close
failures withhold success. Public refusals contain only the failure type and,
where applicable, a fixed stage code; they omit arbitrary child output and
exception text. This enforces the probe's supported transport path; it is not
an operating-system isolation boundary.

The existing installed-Core import contract also requires an explicit runtime
root. The probe supplies a separate empty read-only runtime/database directory
for imports, checks it remains empty, and never admits it as a profile or owner.
This bootstrap directory supplies no source data or financial write authority.

Local fsync/process evidence does not establish power-loss hardware behavior,
independent storage, global latest-N rollback detection or an off-machine
recovery-key channel. Same-UID/root process or tool replacement is outside the
isolation claim. Online recovery admission, durable remote storage and guarded
recovery promotion require separate service evidence and remain unestablished.
