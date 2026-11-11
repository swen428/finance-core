# Native recovery material v1 contract

`finance_core.lifecycle_v2.recovery_material_v1` is an opt-in, bounded API for
collecting and independently checking complete native `lifecycle_v2` history
and its original evidence. It supports only offline-synthetic personal
expenses. The package is unencrypted. A successful check reports
`CONTENT_VERIFIED_OFFLINE`; it does not authorize a writer, restore, admission,
or runtime activation, and it does not prove remote durability, currentness,
independent custody, or protection against rollback of the pinned checkpoint.
This contract assigns no release identity.

## Public API

Import the feature explicitly; the package root and existing lifecycle exports
remain unchanged:

```python
from finance_core.lifecycle_v2.recovery_material_v1 import (
    ClosureCheckV1,
    ClosureFrontierV1,
    ClosureIdentityV1,
    ClosureInventoryV1,
    ClosureLimitsV1,
    NativeClosureV1,
    RecoveryEvidenceArchiveSourceV1,
    RecoveryEvidenceAuthenticatorV1,
    RecoveryMaterialError,
    check_native_closure_v1,
    collect_native_closure_v1,
    decode_native_closure_v1,
)
```

The function signatures are:

```python
collect_native_closure_v1(
    service: LedgerService, *,
    archive_source: RecoveryEvidenceArchiveSourceV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    previous_packages: tuple[bytes, ...] = (),
    limits: ClosureLimitsV1 | None = None,
) -> bytes

decode_native_closure_v1(
    payload: bytes, *, limits: ClosureLimitsV1 | None = None,
) -> NativeClosureV1

check_native_closure_v1(
    packages: tuple[bytes, ...], *,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    expected_identity: ClosureIdentityV1,
    expected_frontier: ClosureFrontierV1,
    limits: ClosureLimitsV1 | None = None,
) -> ClosureCheckV1
```

`RecoveryMaterialError` extends `LifecycleError` and exposes a stable `code`.
The archive source implements `export(kind: str, evidence_id: str) -> bytes`;
the authenticator implements
`verify(*, owner_id: str, key_id: str, payload: bytes, authentication: bytes)
-> None`. Successful verification must return exactly `None`; exceptions,
unknown keys and any other return value reject the evidence. The authenticator
is independently configured with trusted owner/key policy. Material cannot
install a trust root or change that policy. No production signature or
encryption algorithm is specified by this contract.

All returned models are frozen values. Their nested sequences are tuples and
their wire representation has exact fields; unknown fields are rejected. The
complete model fields are:

| Model | Exact fields |
| --- | --- |
| `ClosureIdentityV1` | `source_binding`, `core_identity`, `migration_identity`, `migration_ledger_digest`, `format="native-closure-identity-v1"`, `scope="offline-native-personal-expense-v1"` |
| `ClosureFrontierV1` | `package_sha256`, `event_seq`, `event_sha256`, `format="native-closure-frontier-v1"` |
| `ClosureInventoryV1` | `event_count`, `event_sha256`, `fact_version_count`, `current_facts`, `rows_sha256`, `evidence_sha256` |
| `NativeClosureV1` | `identity`, `previous`, `first_event_seq`, `last_event_seq`, `last_event_sha256`, `rows`, `evidence`, `inventory`, `format="native-closure-segment-v1"` |
| `ClosureCheckV1` | `identity`, `frontier`, `inventory`, `current_values`, `status="CONTENT_VERIFIED_OFFLINE"` |

`source_binding` is the complete frozen `RuntimeBinding`: `instance_id`,
`generation`, `principal_id`, `client_id`, and `environment`. Its environment
must be `offline-synthetic`. `core_identity`, `migration_identity`, and
`migration_ledger_digest` identify the actual source owner and installed Core.
This format requires the complete installed migration ledger 001–061, with the
installed migration identity and ledger digest matching exactly.

`ClosureFrontierV1.package_sha256` is SHA-256 of the complete package bytes;
`event_seq` and `event_sha256` identify that package's final event. An empty
genesis frontier has sequence zero and a null event hash, while its package hash
still hashes the actual empty-segment package bytes. `ClosureInventoryV1` covers
the entire reconstructed prefix through the package endpoint, not only that
segment. Its `current_facts` is an ascending tuple of
`(record_id, version, fact_sha256)` tuples. `rows_sha256` hashes the canonical
seven-table row object; `evidence_sha256` hashes the canonical sorted complete
evidence-entry array. `ClosureCheckV1.current_values` is the ascending tuple of
`(record_id, economic_event_id, version, fact_sha256, commit_seq, source_id,
values_json)` from the independently rebuilt and audited database. It is
computed from that database, never copied from a package projection.

`ClosureLimitsV1` fields and hard defaults are:

| Field | Default hard maximum |
| --- | ---: |
| `max_package_bytes` | 16 MiB |
| `max_chain_bytes` | 64 MiB |
| `max_packages` | 32 |
| `max_events` | 10,000 |
| `max_rows` | 20,000 total across all seven tables, including events |
| `max_evidence_entries` | 10,000 |
| `max_artifact_bytes` | 4 MiB per artifact |
| `max_artifact_total_bytes` | 32 MiB across the complete chain |
| `max_text_bytes` | 1 MiB per source/response/JSON/metadata string |
| `max_json_depth` | 64 |

Callers may lower limits only. Each limit must be an exact positive integer;
booleans, zero, changed types and larger values are refused. JSON depth has a
fixed hard ceiling of 64. Counts and byte limits apply to the full chain, so
splitting material cannot bypass a limit. These are format limits, not a
production capacity, latency or record-volume guarantee.

## Supported history and exact row closure

The closure contains only native `lifecycle_v2` personal-expense history whose
capture event uses the current `lifecycle-capture-v2` generation-bound format.
It rejects imports, archives, `legacy:` sources, generationless captures,
unknown event kinds, other financial types, and unknown or unrepresentable
columns. An applied extension schema may exist in the source profile, but its
rows are outside this package. Other application tables, interaction journals,
pending/unknown runtime state and unrelated finance tables are not copied.

`rows` has exactly these seven keys. Each row has exactly the listed columns,
with SQLite integer, text and null values preserved; JSON text cells remain
their original strings. Arrays are ordered by owning event sequence.

| Key | Exact columns | Owning event |
| --- | --- | --- |
| `sources` | `source_id`, `source_key`, `evidence_id`, `principal_id`, `client_id`, `raw_input`, `original_sha256`, `source_sha256`, `generation`, `created_at` | `capture`, with `entity_id=source_id` |
| `proposals` | `proposal_id`, `source_id`, `version`, `observation_id`, `expected_version`, `record_id`, `values_json`, `observation_json`, `proposal_sha256`, `created_at` | `propose`, with `entity_id=proposal_id` |
| `reviews` | `review_id`, `proposal_id`, `expected_version`, `record_id`, `values_json`, `material_json`, `review_sha256`, `created_at` | `review`, with `entity_id=review_id` |
| `deliveries` | `review_id`, `evidence_id`, `material_json` | `delivery`, with `entity_id=review_id` |
| `facts` | `record_id`, `version`, `economic_event_id`, `source_id`, `review_id`, `values_json`, `predecessor_sha256`, `fact_sha256`, `commit_seq` | `confirm`, with `seq=commit_seq` |
| `decisions` | `action_evidence_id`, `review_id`, `operation_id`, `record_id`, `version`, `material_json` | The same `confirm`, with exact `operation_id` match |
| `events` | `seq`, `operation_id`, `kind`, `entity_id`, `payload_json`, `previous_sha256`, `event_sha256` | The event owns itself by `seq` |

The only event kinds are `capture`, `propose`, `review`, `delivery`, and
`confirm`. Every event must own exactly its required row or rows, and every
row must have exactly one owner. A confirmation owns exactly one fact and one
decision. The package preserves the complete prefix, including unused sources
and proposals, superseded proposals, unresolved reviews, reviews with and
without delivery, every fact version, every decision, and every event. It does
not invent delivery rows for reviews that have none. Missing, orphaned,
duplicate, out-of-order or extra rows fail the whole operation.

Each proposal's original provider request and response remain in its evidence
closure. If `observation_json.expected.reply_evidence_id` is non-null, that
reply source is required even when no `sources` row refers to it. All evidence
dependencies are exact: the archive must contain every required key and no
unreferenced key.

## Evidence archive and authentication

The `evidence` wire value is an array of complete entries sorted by
`(kind, evidence_id)`. Each entry has exactly:

```text
format = "native-evidence-archive-v1"
kind, evidence_id, owner_id, key_id
context, value, artifacts, authentication_b64
```

`kind` is one of `source`, `provider`, `delivery`, or `action`. `context` is
null for source evidence and otherwise the complete expected request object:

| Kind | Exact context fields | Exact value fields |
| --- | --- | --- |
| `source` | null | `evidence_id`, `principal_id`, `client_id`, `source_key`, `raw_input`, `original_sha256`, `source_timestamp`, `timezone` |
| `provider` | `binding`, `source_id`, `source_sha256`, `expected_version`, `projection`, `record_id`, `reply_evidence_id`, `current_proposal_id`, `current_review_id`, `correction_reason`, `current_fact_sha256`, `current_economic_event_id` (`binding` has the five `RuntimeBinding` fields) | `evidence_id`, `source_id`, `principal_id`, `raw_response`, `request_sha256`, `reply_text` |
| `delivery` | `binding`, `review_id`, `review_sha256` | `evidence_id`, `review_id`, `display_sha256`, `principal_id`, `client_id` |
| `action` | `binding`, `review_id`, `review_sha256`, `delivery_evidence_id` | `evidence_id`, `review_id`, `review_sha256`, `delivery_evidence_id`, `principal_id`, `client_id`, `action` |

The `action` value must be `confirm`; source timezone must be
`Asia/Singapore`. All expected DTO fields are present even when their value is
null or an empty projection/reply string. Evidence values and contexts must
match the exact retained lifecycle contract.

Each artifact has exactly `role`, `sha256`, `size`, `bytes_b64`, and `locator`.
Roles are unique and sorted for each entry; their required sets are:

| Kind | Required roles and contents |
| --- | --- |
| `source` | `original` is the exact UTF-8 bytes of `raw_input`; `raw-envelope` is the actual archived capture envelope. |
| `provider` | `request` is canonical UTF-8 of the complete expected invocation; `response` is the exact UTF-8 `raw_response`; `raw-envelope` is the actual archived provider observation. |
| `delivery` | `display` is the exact displayed canonical review material; `raw-envelope` is the actual archived delivery evidence. |
| `action` | `raw-envelope` is the actual archived action evidence. |

Every artifact's declared size and lowercase SHA-256 must match its decoded
bytes. `bytes_b64` uses standard padded canonical Base64. `raw-envelope` must be
nonempty and must have been retained and authenticated at acquisition time;
export cannot synthesize it. `locator` is null or bounded historical metadata
only. It is never opened, resolved, downloaded, or executed. The material embeds
the bytes, including original text and provider response, in unencrypted form.
Artifact Base64 length is bounded before decoding; decoded artifact bytes use
`max_artifact_bytes` rather than the ordinary text limit. `authentication_b64`
is also nonempty standard padded canonical Base64, with a fixed decoded maximum
of 64 KiB and its encoded length checked before decoding. This v1 format does
not support photo, PDF, or path-based source artifacts.

The authenticator verifies canonical UTF-8 of the complete entry after removing
only `authentication_b64`. The authenticated bytes include identity, context,
DTO, every artifact byte, locator, size and digest. Authentication is independent
of the package hash. Owner/key identifiers select already trusted historical
verification material and cannot introduce a new key or root. No missing
evidence may be fetched, synthesized from an expected value, or re-signed during
checking.

During collection, the actual service evidence port audits the source first.
The collector then exports the pre-stored entries and audits the same source
transaction again through an archive-only evidence port. That port performs
exact `(kind, evidence_id)` lookup and compares each provider, delivery or
action request against the authenticated context before returning its DTO. It
has no fallback to the original service port, provider call, file path or
evidence generator. A missing entry or any mismatch rejects the package.

## Encoding, chain and pinned identity

Package bytes use the lifecycle canonical JSON encoding: UTF-8,
`ensure_ascii=False`, sorted keys, compact `,` and `:` separators, and no NaN or
Infinity. Decoding rejects invalid UTF-8, a BOM, duplicate keys, floats,
non-finite numbers, noncanonical integers or escapes, noncanonical whitespace,
unpaired surrogates, excess depth and unknown fields. Every JSON-text column is
also parsed and checked under the strict contract. Integers must be exact
integers rather than booleans, fit signed 64-bit range, and meet their
field-specific positive or nonnegative constraint. SHA-256 is 64 lowercase
hexadecimal characters. Identity strings are bounded to 256 UTF-8 bytes and
cannot contain control characters. Other individual text values are bounded by
`max_text_bytes` without trimming or Unicode normalization.

The first package has `previous=null`, starts at event sequence 1, and contains
the complete prefix. Its only empty form has `first_event_seq=1`,
`last_event_seq=0`, null last-event hash, and no rows or evidence. A later
package's `previous` must equal the actual preceding package's
`(package_sha256, event_seq, event_sha256)`; its first event is exactly the
preceding sequence plus one. Event sequences are continuous. Only the first
genesis package may be empty. Collection with no new events fails with
`no_new_material`. Evidence keys introduced in prior packages cannot be
repeated in a later package, even with identical bytes.

`collect_native_closure_v1` requires an actual `LedgerService`, its matching
`ProductionOwner`, and the existing offline-synthetic `RuntimeBinding`. It does
not accept a database path or caller-provided connection. If
`previous_packages` is supplied, it must be the complete chain from genesis;
the collector authenticates and reconstructs that chain, compares every prior
row and evidence entry with the matching prefix in the same source read, and
emits only newly owned rows and newly introduced evidence. A supplied digest or
manifest alone is not trusted.

Before opening untrusted packages, the caller must independently pin both the
expected `ClosureIdentityV1` and `ClosureFrontierV1`. `decode_native_closure_v1`
only checks structure and canonical encoding; successful decoding is not
authentication, integrity or recovery evidence. `check_native_closure_v1`
requires the full package chain, the independent authenticator, and those exact
pinned values. It accepts no service, source database, owner, evidence port,
path or SQL. An older valid prefix fails against a newer pinned frontier. If an
operator rolls back both the chain and its pinned frontier, this format cannot
detect that coordinated rollback. Collection itself does not prove the anchor
has been kept off-machine or is the latest anchor.

## Rebuild, cleanup and result meaning

Collection runs under the existing protected lifecycle read operation. It
begins one read transaction and keeps scope, capacity checks, schema and event
verification, source-port audit, evidence export/authentication, archive-port
audit, inventory construction and package encoding within that transaction.
The transaction rollback is attempted in `finally`; protected connection close
and control cleanup still run if rollback raises. Rollback, close, cleanup and
owner-witness checks must all succeed before package bytes are returned. Any
failure releases no package or successful frontier.
Collection does not write lifecycle state, create an archive, or perform
network access; callers may store returned bytes after the protected operation
has closed.

The source-side collector follows the existing protected connection policy. It
checks the installed migration/schema identity, complete lifecycle evidence
audit, event-to-row ownership and the foreign-key checks permitted by the
owner; it does not issue an owner-prohibited `PRAGMA integrity_check` or relax
the connection authorizer. Checking creates its own in-memory SQLite database
using only trusted Core migrations and fixed table/column/parameterized insert
constants. The installed 001–061 ledger and schema must match
`expected_identity`. Package fields cannot select SQL or DDL. Rows replay in
event order without replace, ignore, update, delete, trigger/FK disabling or
migration modification. At each segment and at the final endpoint, the
independent checker validates the complete migration history, schema, full
SQLite integrity and foreign keys, lifecycle evidence audit, exact
event-to-row ownership, all seven-table inventory, and personal-expense fields
using the existing Core financial validation. Any failure closes the temporary
connection; connection-close failure cannot yield success. No path, database,
writable connection, admission token or executable recovery object is returned.

`CONTENT_VERIFIED_OFFLINE` means that this complete package chain reconstructs
and audits to the caller's pinned identity and frontier under the supplied
trusted verifier. It does not prove that the package is encrypted, durably
stored, independently replicated, signed with a protected production key,
current relative to a live source, protected against loss of the pin, or
authorized for promotion or writing. Historical confirmation decisions remain
historical evidence; they do not grant new execution authority.

Public error codes include `invalid_material`, `unsupported_scope`,
`material_limit_exceeded`, `missing_evidence`,
`evidence_authentication_failed`, `evidence_context_mismatch`, `chain_mismatch`,
`identity_mismatch`, `frontier_mismatch`, `inventory_mismatch`, and
`no_new_material`. Failure is all-or-nothing: no partial inventory, skipped
entry, older-frontier fallback or reduced result is returned. Existing owner
and protected-close exceptions retain their established semantics.

## Required validation and installed-package proof

Acceptance must exercise the built wheel and source distribution from isolated
installations outside the source tree. Import `recovery_material_v1` from each
installed artifact and run a strict-evidence, two-segment round trip using the
actual lifecycle service and synthetic data. The test must create the lifecycle
through guarded capture, proposal, review, delivery, action and confirmation;
include at least two currencies, a correction to version 2 of one event, and
an unconfirmed delivered review. The first segment ends after the original fact;
the second includes the correction and new unused events. Compare the checked
inventory and current values with the source-side inventory and effective reader.
All Core module paths must resolve to the installed artifact.

The fixture must pre-register all evidence during acquisition, including
request/response/original bytes, original envelopes, delivery and action
evidence, and any independent reply source. Each archive lookup must reject an
absent key; the archive port must have no dynamic generation or fallback path.
After collection, disable the source port and make the original database
unavailable. The installed checker must succeed using only the complete package
chain, the independently configured authenticator, and pinned identity/frontier.

The focused source suite additionally covers separate unused sources, proposals,
superseded proposals, and reviews with and without delivery. Confirmation retries
must not create new events. It compares all seven tables, every retained historical fact,
decision and event, all original and envelope bytes, complete evidence inventory,
and current values against the source-side audit vectors. It verifies that one
economic event has only its latest current version while its earlier versions
remain present. It must show that deleting any dormant row, prior fact,
decision, event, reply source, raw envelope, original, request or response
fails without consulting or regenerating source evidence.

Negative checks cover altered same-ID evidence, actor/key/owner/context/raw
response/amount, changed identity or schema, wrong binding or scope, broken or
missing chain segments, duplicate rows or evidence, wrong sequence or
predecessor, old pinned prefix, unsupported fields/kinds, malformed or
noncanonical JSON/UTF-8/Base64, and each limit boundary (exactly-at-limit
accepted, one-over refused), including cumulative chain limits. Limits may be
lowered to keep boundary fixtures small. Monetary comparisons use the existing
Money and effective-ledger validation rather than a second financial
calculation. A decoded package alone must never count as checked. Collection tests
must exercise the protected rollback/close/cleanup failure path and prove that
no bytes escape when closure is uncertain. Checker failure must close its
temporary database, and its result must not change any instance ACTIVE/HELD
state or supply a usable restore profile.

Install compatibility checks preserve existing package imports and APIs, the
001–061 migration ledger, rejection of non-offline `RuntimeBinding` values,
`SnapshotCut` v3 behavior, and the directly related existing lifecycle,
effective-ledger and recovery regressions. The proof assigns no package version,
release identity, runtime activation or production-readiness claim.
