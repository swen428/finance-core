# Publication review v1

This repository was assembled in a new Git history. It does not inherit pull
requests, issues, Actions logs, artifacts, commits, or deleted objects from the
private runtime repository.

## Included boundary

- Deterministic Finance product code, migrations 001-048, synthetic tests,
  package resources, generic Finance Bridge code, and build metadata.
- Apache-2.0 project licensing and third-party package notices.

## Excluded boundary

- Runtime databases, receipts, statements, attachments, exports, backups,
  credentials, OAuth profiles, tokens, owner state, real logs, and operational
  evidence.
- Private delivery governance, protected-merge evidence, OpenClaw platform
  patches, platform receipts, and provider/runtime activation configuration.

## Review notes

- Synthetic people and merchants use role labels such as `Owner`, `MemberA`,
  and `Example Restaurant`.
- Local paths use synthetic locations only. Secret-shaped strings occur only
  in negative tests that prove they are rejected or redacted.
- Twelve immutable historical migration comments contain the project owner's
  given name in a warning not to modify live data. They contain no account,
  transaction, credential, contact, or runtime data. The comments remain
  byte-for-byte unchanged because migration checksums are an audit contract;
  all non-immutable examples have been anonymized.
- The native Swift helper is original project source. No compiled native
  binary is committed.
- The generic Bridge declares third-party dependencies but contains no
  vendored OpenClaw runtime or third-party source patch.

Visibility must remain private until the independent code, artifact, license,
and secret reviews complete and the owner separately confirms publication.
