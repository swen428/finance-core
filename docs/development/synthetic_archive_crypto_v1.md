# D4 synthetic archive crypto proof v1

This module is an independent, bounded bytes-in/bytes-out proof of the D4
archive-key design. Import `finance_core.synthetic_archive_crypto` explicitly;
it is not registered as a backup, restore, CLI, or package-level product entry.
It accepts no Finance database, profile, attachment, archive pathname, or
output directory. Its callers must supply already synthetic bytes and a trusted
`ArchiveContext`, `AgeToolPin`, native age recipient/identity, and disposable
32-byte authentication key. The result is two byte objects, with no durable or
atomic publication guarantee.

The fixed `FCD4AGE1` plaintext frame contains two unsigned 64-bit lengths,
SHA-256 digests and the exact manifest and archive bytes. The manifest remains
opaque: its assertions, member paths, cut consistency and evidentiary meaning
are not checked here. The entire frame is encrypted by native X25519 age
v1.3.2. The canonical JSON sidecar contains only format, archive/context IDs,
key epoch, recipient/auth-key IDs, ciphertext length/digest, and a
domain-separated HMAC-SHA256 of those fields. The HMAC key is separate from
the age identity. `open_synthetic_archive` matches the caller's trusted
expected context, ciphertext length/digest and HMAC before passing ciphertext
or identity to age. The tool's secret-free `--version` admission runs first so
a changed tool fails before the HMAC key is used. It returns plaintext only
after age exits successfully and the complete frame
passes length and digest checks. Unknown, duplicate or noncanonical sidecar
fields fail. The caller's trusted key registry must map each key ID to its
actual recipient, identity and authentication key; this primitive authenticates
those declared IDs but does not create or verify that external registry.

The synthetic limits are 8 MiB for archive bytes, 1 MiB for manifest bytes,
9 MiB plus framing for decrypted output, an additional 1 MiB for ciphertext
overhead, and 4 KiB for the sidecar. Child stdout/stderr and execution time
are bounded. A failed child, late decryption failure, timeout or oversized
output is terminated/reaped and yields only a generic exception; subprocess
output and key material are never placed in exceptions. The age identity is
passed through an inherited anonymous pipe at `/dev/fd/N`, while payload and
ciphertext use stdin/stdout. No shell or key-bearing command argument is used.

`AgeToolPin` must come from a trusted installed-artifact record, never from
the encrypted envelope. It requires an absolute executable path, the current
platform, release `v1.3.2` and its exact SHA-256, rechecked before each
invocation together with `--version`. The executable must be a non-symlink,
single-link regular file owned by root or the current user and not writable
by group or others. A hash check does not prevent a
concurrent replacement of that path between validation and process launch:
this proof requires a trusted stable tool installation. It does not install
age, manage the owner-controlled secret store, or establish off-Mac key
custody. No production recipient, identity or authentication key is embedded
in source or tests.

The offline tests use a deterministic fake CLI solely to exercise wrapper
behavior. An optional local test uses an explicitly selected, previously
verified native age binary and generates disposable keys in memory:

```text
FINANCE_D4_TEST_AGE_BINARY=/absolute/path/to/age python -m pytest -q tests/test_synthetic_archive_crypto.py
```

The normal CI run needs neither the tool nor network access. Success here
cannot set `encrypted_local_verified` or `restore_verified`, establish a
recoverable point, authorize retention pruning, complete D4-4, or unblock
managed SQLite admission. The full D4 pipeline still requires a complete
Host/Bridge/Core cut, manifest validation, durable ciphertext/sidecar pair
publication, independent key custody, isolated restore and a final-candidate
clean-VM rehearsal.
