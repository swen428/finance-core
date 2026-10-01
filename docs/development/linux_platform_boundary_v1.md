# Linux platform boundary v1

This boundary adds generic Linux profile permissions and platform-artifact
verification to the existing Mac support. It does not install/activate a Host,
select credentials, grant production database authority, perform OCR, publish a
release or establish full-profile backup/recovery. All acceptance data is synthetic.

## Fixed layouts and authority

The original Mac API/layout remains compatible. Linux uses an owner-selected
canonical absolute data root ending in `finance-codex` and fixed `profiles/<id>`
children. Profile locators are trusted local configuration, not message inputs.
The blank witness cannot open a populated database or authorize a writer.
Enrollment and subsequent managed access retain registration, private permissions,
descriptor revalidation and the existing profile gate/SQLite lifetime rules.
Generic staging creation and opening reject the Linux managed namespace even
without registration. Both layouts reject ordinary staging access beneath
registration or pending markers, including renamed trees and corrupt markers;
markers signal refusal, never authority. The original Mac fixed-path reopening
refusal remains in force, while unregistered Mac generic creation retains its
existing behavior. Legacy backup/migration reject both complete managed
namespaces before file effects.

Both Core and Bridge reject Linux access/default POSIX ACL attributes using
actual filesystem inspection; mode bits alone are insufficient. Missing ACLs are
allowed only on ENODATA; unreadable or unsupported inspection fails closed.
Darwin's original allow-entry refusal and deny-only compatibility are unchanged.
Revalidation must not close a second descriptor for an active SQLite main file.

## Platform artifact identity

Supported build identity pairs are `darwin/arm64` and `linux/x64`, with the fixed
Node version from the package. The installed environment, platform receipt and
both Finance POSIX and fs-ext native bindings must agree. Every existing
artifact-tree/hash/mode/size, source provenance, Host patch, audit and optional
peer check remains in force.

Mac retains adhoc signature receipts and actual codesign verification. Linux
receipts explicitly use `platform: linux-x64`, `signature: not-applicable` and
`codesign_verified: false`; they cannot claim a Mac signature. The actual native
bytes must identify ELF64, little-endian x86_64 shared objects. Wrong platform,
architecture, format, truncated header, forged signature or changed bytes fail.
The existing receipt schema is preserved; this is an additive explicit branch.
A synthetic receipt demonstrates verifier behavior, not a real Linux Host build.

Reproducibility still builds the native bindings at distinct paths. Source-only
committed build provenance stays platform-independent so both OS lanes can
check the same source identity; native binaries are separately platform-bound.
No existing release/tag/artifact identity is replaced by this capability.

## Acceptance evidence

Focused Mac validation must preserve original path, ACL, gate, codesign and
coordinator/worker regressions. Actual Ubuntu 24.04 x86_64 CI must prove Linux
blank validation, synthetic enrollment/reopen, gate and Core snapshot/independent
reader; refusal before generic staging or legacy backup effects; real access,
masked/default ACL refusal, ACL errors and changed permissions; and both real
native bindings' loading, identity and distinct-path reproducibility.

Injected platform fixtures are logic evidence only. They do not replace actual
Linux kernel/build proof. The Linux Python and Bridge validation lanes assert
Ubuntu 24.04 x86_64 directly instead of assuming the `ubuntu-latest` alias will
always retain that identity. Mac build validation remains required when Bridge
inputs change. Complete candidate Hosted CI, three independent specialist
reviews and protected delivery remain the release-validation prerequisites.
