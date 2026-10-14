# Independent posted-transaction corrections v1

This feature adds a guarded way to correct an independently posted personal
expense after the original posting has finalized. It preserves the original
posting and records each correction as a new, hash-linked version. Callers read
the effective values through `IndependentCorrectionService`; they should not
rewrite the original transaction, source, posting decision, or receipt evidence.

## Application contract

Create `IndependentCorrectionService(posting_service, *,
human_decision_authority=None, clock=None)` with the fixed `PostingService`
that owns the same SQLite connection and trust binding. The four application
operations are:

- `read_effective(transaction_id)` verifies the original independent posting
  and the entire retained correction history in one SQLite read snapshot. With
  no correction, the result is version 0 and reflects the original posting. A
  successful read describes the snapshot it observed; a later correction may
  become current after that read completes.
- `prepare_correction(transaction_id, patch, reason)` accepts changes to
  `amount`, `currency`, `transaction_date`, `merchant`, `description`, and
  `category`. The patch may be partial, while the reviewed projection always
  contains the complete six-field result. Unpatched values preserve the exact
  original stored value; only supplied text is normalized. A currency change
  requires an explicit amount. Preparation does not create a financial effect.
- `submit_correction(plan_id, decision_record_id)` consumes one independently
  verified correction decision and commits the next version, audit event,
  and—when the original was receipt-backed—new receipt facts and an
  authoritative snapshot. `submit` is a short alias.
- `recover_correction(plan_id)` returns `None` for an uncommitted plan. For an
  applied plan, it returns both the immutable version produced by that plan
  and the latest current version, so a retry remains useful after later
  corrections. `recover` is a short alias.

The reconciliation adapter has a separate read-only composition seam:
`read_effective_in_snapshot(connection, transaction_id)`. It accepts only the
same owning connection while a caller-owned SQLite snapshot is active, so the
adapter reads original posting and correction history consistently without
taking transaction ownership or adding another correction operation.

The returned effective transaction distinguishes `version` and `fields` from
its immutable `original_source`, and retains the full correction history. A
`PostingService` configured with the same correction authority also reports
`applied` (version 0) separately from `current` in its posting status. That
status still proves the original posting; it does not rewrite it to the latest
correction.

## Human approval and recovery

The human decision authority verifies durable signed source, full displayed
plan, direct private reply, identity, action, expiry, and consumption evidence
from the same SQLite connection. Its typed decision and dedicated correction
schema bind to the full plan projection. It uses the fixed trusted binding's
decision key identity, but ordinary posting decisions do not authorize
corrections and correction decisions cannot authorize postings.

An already finalized original posting is eligible even if its posting
coordinator has not yet advanced its stage marker. Submission repeats
original-source, predecessor, approval, and receipt checks inside
`BEGIN IMMEDIATE`. Competing plans for the same predecessor cannot both
become current. Replaying the same plan and decision returns its original
applied version and the latest current version; reusing that plan with another
decision conflicts. Pre-commit failures leave no partial version, receipt, or
audit record. A response lost after commit can be resolved with `recover()` or
by reopening the database and reading the same plan.

Correction rows and financial audit events are append-only. Reads fail closed
if a version, decision, receipt fact, snapshot, or audit-chain entry is missing
or inconsistent. Receipt-backed corrections create a new immutable snapshot.
The first correction links to the original snapshot, and each later correction
links to the immediately preceding correction snapshot; the original receipt
and snapshot remain verified throughout.
Text corrections do not create receipt snapshots.

## Storage and validation boundary

Migration `060_independent_corrections.sql` adds the correction targets,
plans, versions, consumed decisions, and receipt-fact tables. It is additive to
the existing migration ledger; older migration bytes and the package version
remain unchanged. The migration contract resource pins the current complete
ledger inventory and digest.

The automated correction acceptance tests use temporary SQLite databases,
the repository's real signed synthetic independent-posting fixtures, and
separate HMAC-signed synthetic correction display/reply/decision records. They
exercise no live finance database, real human approval provider, Telegram
runtime, or production correction flow. The service remains a library
boundary; this change does not activate a runtime integration.
