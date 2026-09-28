# D4 synthetic native-authority component v1

This component proves a bounded local authority log over synthetic files. It is
not a SQLite VFS, profile issuer, backup or restore tool, managed-open grant,
production enrollment adapter, or durability guarantee. `main.witness` contains
opaque synthetic sentinel bytes; this component never opens SQLite or treats
the sentinel slots as database journals or WAL files.

The public package has no top-level re-export or production caller. Test issuer,
fault controls, cold-process worker, and reference model stay under `tests/` and
are excluded from the wheel. A successful component test is
`SYNTHETIC_COMPONENT_PROOF`; it does not mean `U1_CLOSED`, `G2_PASSED`,
`MANAGED_READY`, `RESTORE_VERIFIED`, or `RECOVERABLE_POINT`.

## Fixed byte contract

Every object uses one fixed-size frame:

```text
magic[8] || payload_length_u32_be[4] || canonical_payload || zero_padding || mac[32]
```

The MAC is HMAC-SHA256 over `domain || frame_without_mac`; the frame digest is
SHA-256 over all frame bytes, including the MAC. Payloads are canonical ASCII
JSON with sorted keys and compact separators. Parsers reject alternate framing,
nonzero padding, partial or extra frames, unknown or duplicate keys, invalid
types and ranges, and an invalid MAC. Integers are exact integers; booleans do
not count as integers. File and collection bounds are checked before they can
cause unbounded reads or allocations.

| Object | Magic | Size | Domain |
| --- | --- | ---: | --- |
| Capability descriptor C | `FNA1CAP\n` | 16,384 | `finance-core/synthetic-authority/v1/capability\0` |
| Registry R | `FNA1REG\n` | 4,096 | `finance-core/synthetic-authority/v1/registry\0` |
| Committed head H | `FNA1HED\n` | 2,048 | `finance-core/synthetic-authority/v1/head\0` |
| Enrollment receipt E | `FNA1RCP\n` | 2,048 | `finance-core/synthetic-authority/v1/receipt\0` |

C fixes the synthetic purpose, enrollment identity, directory witnesses, object
roles and names, owner/mode/link policy, role-specific operations, capacity
limits, and barrier profile. A separate immutable test anchor binds the
enrollment identity, C witness and digest, and advertised root-chain witnesses.
The disposable MAC key and anchor arrive through a bounded test-only channel;
they are not read from the target tree. The implementation does not derive
authority from a directory scan or accept a caller-selected path, expected
head, raw FD, or `verified` boolean.

Registry and head files are distinct, append-only files. Each accepted event
adds exactly one R/H pair at the next sequence. Both full files must replay with
valid MAC chains, contiguous sequence, legal transitions, exact pair agreement,
and exact EOF before a snapshot can be returned. A partial, extra, mismatched,
or substituted object is quarantined without truncation, prefix salvage, or
recreation.

Before and after protected effects, the store validates its lease, every pinned
directory and advertised path, the fixed object names, the actual FDs, accepted
log offsets/head, and descriptor/anchor bindings. An operation checks the
actual object it is about to use. A pre-effect mismatch refuses without changing
the original or replacement bytes and poisons the owner. If a later check fails
after a legitimate effect, the operation does not report success and preserves
the effect already completed; it does not claim rollback.

## Transitions and capacity

Only `JOURNAL` and `WAL` kinds are accepted. A fresh store first commits
`GENESIS`. Allocation follows `RESET_INTENT` → reset the admitted actual slot FD
and synchronize it → `RESET_DONE` → `ACTIVE` → token publication. Retirement
commits a generation-specific `RETIRED` event. Restart never resumes an
interrupted reset. A complete ACTIVE generation reopens without resetting its
slot. Lost acknowledgement after a complete pair resolves to the same event;
it is not appended or executed twice.

The pair limit is the minimum of the two record caps and each byte cap divided
by its fixed frame size:

```text
N = min(registry_record_cap, head_record_cap,
        floor(registry_byte_cap / 4096), floor(head_byte_cap / 2048))
```

The reservation model is fixed: ABSENT and RETIRED reserve 0 future pairs,
ACTIVE reserves 1 for its own retirement, RESET_INTENT reserves 3 for
RESET_DONE/ACTIVE/RETIRED, and RESET_DONE reserves 2 for ACTIVE/RETIRED. Before
writing RESET_INTENT, an allocation must have room for its three remaining
allocation events and its future retirement, while preserving every other
active kind's reservation. Retirement can consume only its own reserved pair.
Capacity failure happens before any log append or slot reset.

## Independent evidence and case inventory

Tests compare the store's results with `tests/helpers/synthetic_native_authority_oracle.py`,
which does not import the production package. It computes capacity from fixed
public frame sizes, applies the frozen reservation table, and records bounded
file bytes, hashes, types, modes and device/inode witnesses. Assertions use
original and replacement bytes and operation effects; an exception by itself
does not count as proof. Fault cases bind each hit to the injected role,
device, inode, sequence and phase.

| Case IDs | Exercised inputs | Independent pass observation |
| --- | --- | --- |
| `FMT-001`–`FMT-003` | Independent C/R/H/E frame vectors and exact bytes; MAC domain separation; duplicate/unknown fields, boolean-as-integer, malformed ID, invalid event enum/generation range, truncated/extra/wrong-magic/nonzero-padding/bad-MAC input. | Expected bytes come from the test wire description; invalid frames fail without prefix salvage. |
| `P1-COLD-001`–`P1-COLD-002` | A seed process commits both kinds, exits, then a new interpreter receives only the root, immutable anchor and key on a private channel. A second case copies an otherwise byte-identical tree to new root/file inodes. | The parent recomputes the final sequence/state and H digest from the bounded logs. The copied tree is rejected; source and target bytes and identities remain unchanged. |
| `P1-V2-001`–`P1-V2-005`, `P1-ACTIVE-001`–`P1-ACTIVE-007` | Active-token main witness write/new-open; registry/head substitution; descriptor/receipt/slot substitution; advertised parent and authority/slots directory swap; original C inode truncation during write/sync/query; wrong actual main FD after its pathname is restored. | Original and foreign bytes, opened FD dev/inode, and tree inventory are observed. Active owner mismatch is sticky `PoisonedError`; pre-owner admission mismatch is `QuarantinedError`. |
| `P2` crash-worker tests | Real parent-supervised `SIGKILL` at GENESIS and allocation R/H append, barrier, namespace-postcheck, publish and acknowledgement boundaries, including every 1 KiB fragment of R/H writes. The cold process accepts only a complete matching old/new pair or quarantines the remaining evidence. | The parent kills and reaps the worker and checks file bytes, frame counts, and cold replay. These are process-kill results only, not power-loss or stuck-kernel-fsync results. |
| `P2` modeled-fault tests | Separately inject zero/positive-short writes, registry fsync failure, head-read failure, torn R, dropped H and reordered H. | A failed R barrier never starts H; short writes finish inside one live call. Torn, dropped and reordered bytes quarantine unchanged. Modeled outcomes are not counted as OS-kill evidence. |
| `P3-RESERVE-001`–`P3-RESERVE-004`, `P3-BOUNDARY-001`–`P3-BOUNDARY-002` | Both kinds ACTIVE; both retirement orders after capacity refusal; independent registry/head record and byte limiters; one-byte-under refusal. | The independent reservation oracle agrees. Refusal preserves logs, slot bytes and committed-WAL sentinel; each ACTIVE kind retains its own retirement pair. |
| `P3` crash-worker tests | Real `SIGKILL` around actual slot admission, reset and barrier for JOURNAL/WAL and virgin/retired slots, plus lost ACTIVE acknowledgement. | Evidence names the selected role and observed bytes. Pending reset reopens as quarantine; complete ACTIVE replay returns the same generation without resetting the slot. |
| `P4-REPLAY-001`–`P4-REPLAY-023` | Extra H byte, partial H frame, bad R MAC, authenticated missing field, R/H mismatch, illegal transition, state-digest mismatch, sequence gap, duplicate event ID, generation mismatch/range, op-ID reuse, slot mismatch, phase-order error, epoch mismatch, invalid enum/ID, extra or duplicate field, missing H, one-sided R/H rollback, and logs from another epoch. | Every invalid or one-sided case returns `QuarantinedError`; bounded tree observations prove no truncation, recreation, or rewrite. `P4-ROLLBACK-001` intentionally shows the counterexample: a complete authenticated R/H pair rolled back to the original GENESIS pair is accepted at seq 1, so it does not prove freshness detection. |
| `P4-COLD-SPECIAL`: `test_p4_cold_reopen_refuses_special_registry_without_touching_either_inode[fifo]`, `test_p4_cold_reopen_refuses_special_registry_without_touching_either_inode[symlink]`, and `test_p4_cold_reopen_refuses_special_registry_without_touching_either_inode[hardlink]` in `tests/test_synthetic_native_authority_io.py` | Cold subprocess reopen with the registry path replaced by a FIFO, symlink to the preserved original inode, or hardlink to that inode. The subprocess has a three-second timeout. | Each variant returns `QuarantinedError` without hanging; `lstat` witnesses for the original and replacement entries and the original file bytes remain unchanged. |
| `P5-MODEL-001` | Independent `ReferenceMachine` checks GENESIS, JOURNAL generations, both slot indexes, retirement, stale-token rejection, retained references, fallback selection and alternation after release. | Store snapshots match the test-owned transition/selector history; stale write/retire attempts leave the complete tree unchanged. |
| `P5` owner tests in `tests/test_synthetic_native_authority_crash.py` | A live owner blocks another process until clean close; a fork child cannot use the parent owner and the parent remains active. | Lease refusal preserves bytes, clean close allows cold replay, and forked use fails without changing the parent's owner or tree. |
| `P5-IO` tests in `tests/test_synthetic_native_authority_io.py` | Slot short writes, zero-progress/ENOSPC, short read, fsync error, token/slot limits, injected operation deadline, cross-thread and reentrant calls, uncertain close before/after kernel close, and forced FD-number reuse. | Exact slot/log bytes and follow-up behavior are observed. The owner remains blocked until its process exits; a new process then replays the same active generation. The deadline is a modeled application guard, not a kernel guarantee that `fsync` returns. |
| `P6-FRESH-001`–`P6-FRESH-006` | Fake issuer cancellation/unavailability before enrollment; add-only conflict; unavailable or mismatched readback; ordinary reopen with no external item/receipt; and a nonempty `authority` name collision. | Pre-prepare failures leave the gate-only tree unchanged. Later failures preserve exact C/R/H/E bytes with empty E; ordinary reopen quarantines without repair. A nonempty target reaches neither add-only nor readback. |
| `P6` enrollment crash-worker tests | Real `SIGKILL` around both gate lock/release phases, fake add/readback, descriptor and receipt fragments/file barriers, and authority/slots/root or receipt-directory barriers. The fake issuer checks both leases are released before external calls and publishes immutable anchor material only after add/readback. | External-only hook witnesses have `dev`/`ino` null; the descriptor inode is checked separately. Partial E quarantines with bytes unchanged. A complete authenticated E can be accepted by cold replay even when killed before its file or directory barrier; this establishes process-crash visibility only, not power-loss persistence. Before add/readback there is no published immutable anchor, so no ordinary cold-reopen pass is attempted. |
| `P6-RESTORE-001`–`P6-RESTORE-011` | Replay copied old C/R/H/E bytes at distinct inodes, verify a separately authenticated synthetic cut, then obtain a new target/epoch. Reject cut/log/old-key corruption, reuse of old key bytes or key/registry/enrollment IDs, cut-key reuse of either epoch key, and complete pending RESET_INTENT history. | The target receives only a new grant; no old token is issued. The source archive tree and target-before state remain unchanged on refusal. This authenticates only synthetic historical bytes and a test sentinel. |

The listed cases are the bounded synthetic-only P1–P6 acceptance matrix; they do
not claim exhaustive Cartesian coverage of every field/range or every history.
The coherent-pair rollback boundary is tested explicitly by
`P4-ROLLBACK-001`: restoring a complete authenticated old R/H pair to the same
root and inode identities is accepted at seq 1. Without an independent monotonic
witness, that history cannot be distinguished from the current one, so this is
an expected counterexample rather than rollback detection.

The following remain `NOT RUN` or outside this component's claim: exhaustive
Cartesian mutation of every field/range and every generation/operation-ID
history; a real kernel-blocked `fsync`; actual power-loss and hardware-flush
behavior; and interactions with a production external provider or Keychain.
Before fake add/readback publishes an immutable anchor, there is no anchor for an
ordinary cold reopen, so that scenario is not attempted. These scope limits do
not mean the enumerated synthetic-only cases are missing; a green run proves
only the cases and observations listed here.

## Platform and claim limits

The selected positive filesystem profile requires an explicit no-grants ACL
check. Darwin is the only supported positive platform for this component proof.
Linux refuses with `UnsupportedPlatformError` before target effects; Linux CI
may run format-only tests and refusal checks, but its skipped/refused positive
cases are `NOT RUN`, not a cross-platform component pass. Positive cases in this
inventory must run on Darwin and retain case-level results. A passing run
supports only the enumerated synthetic-only claims and does not extend them to
the `NOT RUN` environments and behaviors above.

Real SQLite, APSW, native extensions/VFS, database reads, schema creation,
Bridge/Host, real or seed financial data, production Keychain, application
support profiles, archive restore, and runtime activation are `NOT RUN` and
outside this component. N01–N07 native SQLite acceptance is also explicitly
`NOT RUN`. Process-kill tests do not establish power-loss behavior, hardware
flush semantics, whole-state rollback detection, arbitrary same-user compromise,
or production enrollment. A coherent restoration of all local authenticated
files remains outside the freshness claim.
