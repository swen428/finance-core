# Verified legacy export contract

Approved feasibility scope: a fixed read-only adapter for the independent
lifecycle at legacy baseline `d8186832f10a4df0af401a7052d0f41980c535e2`, migrations
001–060. This is a high-risk persistence/evidence boundary. It does not modify
old financial owners, historical migrations, monetary rules, or any real data.

`LegacyReadAdapter` is composed from the actual old `PostingService`,
`AmendmentService`, and `IndependentCorrectionService`, all bound to one admitted
connection, instance, source verifier and AI lineage owner. Installation supplies
the fixed original attachment root and explicitly declared external authority
tables. These declarations are trusted composition, not fields accepted from an
export request. The adapter checks the exact Core migration ledger and schema;
consumer-owned authority tables are excluded only from the Core schema fingerprint
and remain fully retained in the archive.

Within one read snapshot it checks every retained source through the old trusted
source port, every AI attempt through the actual historical AI/provider/input
owner, accepted human amendments through their actual retained display/reply
authority, every posting and correction through their existing owners, and audit
chains. Unused amendment and posting cards are reconstructed from the actual
versioned proposal payload, source and historical ancestor lineage. Pending
correction plans must bind an actual verified predecessor, original source,
before-values and version. Receipt financial projections and pending receipt
material use the unchanged calculators/receipt owner. Rehashing a false display
or predecessor does not satisfy these comparisons.

Unused cards and plans are proposals with validated historical context. They are
explicitly `archived_non_executable`; checking them does not assert that a human
approved them. No old card, unconsumed reply, pending plan or archived provider
attempt receives authority in the new lifecycle.

`VerifiedLegacyExport` is an immutable, process-local receipt. Its constructor is
closed and the import guard requires the exact object identity issued by this
verifier. A dictionary, copied dataclass or self-hashed JSON is not an export
receipt. A separately retained `LegacyExportVerifier` trust root authenticates
the complete archive and original inventory using a domain-separated HMAC. Its
key is supplied by installation/recovery composition and is never decoded from
an archive or imported approval field. A reader rebuilt after restart must
receive that trusted verifier independently. This follows the repository's
cooperative Python trust boundary and does not claim resistance to hostile code
running in the same interpreter.

The receipt retains:

- Explicit schema, old baseline, namespace, instance and snapshot identity.
- Original and current six financial fields, real correction versions, original
  source/display/decision material and source identity for each confirmed fact.
- A complete lossless **logical** old database archive: every table, column, row
  and schema object, including unexecuted interaction state and authority journals.
  SQLite REAL and BLOB values use tagged hexadecimal forms. This is not a claim
  that the original SQLite file bytes or page layout have been copied.
- Every declared attachment's checked original bytes, path, hash and length.

The export has no network or transport operation. Source databases are compared
before and after verification. Unknown old owner formats, D1 human drafts, legacy
correction owners, unowned financial facts and incomplete accepted postings fail
closed and require a separate explicit adapter/recovery decision. The supported
coverage label does not claim that arbitrary historical databases migrate.

`LedgerService.import_verified` owns the new admitted write transaction. The
import helper accepts only issued, independently sealed exports and preserves
every original and correction version. Its economic identity is the old
namespace, old instance and old fact ID; repeating an export, or receiving a
different export containing additional pending interactions, cannot duplicate
that economic fact. Old approval IDs are retained as history, never relabelled
as new human approval. No imported draft, card or plan is inserted into the new
executable proposal/review/decision tables.

The complete archive is persisted in `l2_imports`. Small per-source and per-record
HMAC capsules in `l2_archives` bind the exact imported text, original values,
version chain and new target identity to that snapshot. Ordinary indexed reads
check these independently authenticated capsules without scanning all old unused
cards. Full audit and snapshot creation additionally verify the entire retained
archive and every original byte. A fresh current correction requires a new
authenticated reply, provider result, full card and human action under the new
owner; it does not execute an archived old plan.

Impact classification:

| Surface | Treatment | Evidence |
| --- | --- | --- |
| Old write owners and migrations 001–060 | inspect only | Real existing owner fixtures |
| Old source/human/provider contracts | inspect and reuse actual verification | Missing retained-proof negatives |
| Unused historical card reads | new fixed export comparison | Rehashed display/payload negatives |
| Pending correction archive | new authenticated predecessor comparison | Rehashed before/source/version/predecessor negatives |
| Original retention | copy checked bytes to immutable receipt | Real local OCR receipt and missing original negative |
| New lifecycle import writer | explicit follow-on integration | Actual ProductionOwner, replay/dedup, restart and wrong-root proofs |

The focused synthetic suite is `tests/test_lifecycle_v2_legacy.py`. Its legacy
fixtures explicitly apply the genuine 001–060 migration prefix, generate actual
old deterministic/AI posting and human edit/correction effects, and retain signed
synthetic external source/provider/human evidence with explicit test trust roots.
It does not seed facts directly or substitute a `verified=True` port. No real
database, provider service or Telegram runtime is inspected or invoked.

## Focused evidence, 2026-10-24

- Export-only first gate: 17 cases passed in 79.72 seconds.
- Export plus initial new-owner integration: 18 cases passed in 96.10 seconds.
- After the final archive-inventory/source guards and the explicit correction
  reason contract, the two affected new-owner integration cases passed in
  18.60 seconds. These also cover receipt import, deletion of the old original,
  closing the old connection, and exact original bytes retained in the new
  independently readable SQLite snapshot. The final file contains 19 cases;
  the preceding results are separate executions, not a claim of one 19-case run.
- Focused Ruff and Mypy checks passed for the new adapter and its tests.

These are offline component and integration proofs. The old fixture uses the
existing admitted synthetic staging connection and genuine old financial owners
with retained signed source/provider/human evidence. The target uses actual
ProductionOwner sessions. This does not newly benchmark old production-reader
composition, inspect real history, certify unknown legacy formats, or replace
the final candidate's independent specialist reviews and hosted validation.
