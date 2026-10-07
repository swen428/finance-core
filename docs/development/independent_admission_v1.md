# Independent source and human-decision admission

The Application admission boundary checks evidence supplied by trusted service
composition without importing Telegram, OpenClaw, a provider, or a platform
adapter. It separates the identity of a submitting client from the human
principal who can decide on an exact financial review.

This is an evidence-inspection boundary. A successful inspection does not
confirm a proposal, consume a decision, convert a receipt, create a transaction,
or grant posting permission. The existing financial services retain those
responsibilities. There is no new transport, runtime registration, migration,
key creation, public network endpoint, or production activation.

## Trusted composition and operational input

Import `AdmissionService` and `TrustedBinding` from
`finance_core.application.admission`. Construct the service with keyword-only
`connection`, `source_verifier`, `human_decision_authority`, `binding`, and
`clock` arguments. Its operations are:

- `admit_source(intake_public_id)` returns a frozen `SourceInspection`.
- `check_human_decision(proposal_public_id, decision_record_id)` returns a
  frozen `DecisionInspection` for the exact `confirm` action.

Service construction fixes the database connection, trusted source verifier,
human-decision authority, identity/key binding, and clock. Operational calls
provide record references only. A request cannot choose a database path,
verifier, key, human actor, adapter, module, or policy. An ordinary dictionary
containing `approved=true`, a claimed actor, or an old delivery receipt is not
verified evidence.

The source verifier and decision authority are trusted application code. They
must read durable evidence and verify its integrity and adapter-specific
authenticity on every call. Their typed return objects report that verification;
constructing a dataclass is not authentication. The Application independently
checks the returned type, schema domain, configuration binding, record
identities, hashes, and validity interval. Unknown, incomplete, unavailable,
or conflicting evidence must refuse admission.

Python interfaces and the dependency guard are not a sandbox against hostile
code in the trusted process. A future transport adapter must earn its own
authenticity, durable-capture, card-delivery, and human-action proof before it
can be wired into this composition. Synthetic test adapters do not qualify as
production adapters.

## Snapshot and decision binding

Source and decision inspection reuse the existing Application read snapshot.
The caller owns the SQLite connection. Caller-owned work is never committed
or rolled back by this entry, and the entry does not perform a database write.
Ports must honor the same connection and read scope. A network lookup, mutable
in-memory approval cache, or evidence from another database cannot substitute
for that persisted readback.

Both ports implement `verify_persisted`; the decision port additionally receives
an internally constructed `ExpectedHumanDecision`. `VerifiedSource` and
`VerifiedHumanDecision` are port return types, never operational request types.
`TrustedBinding` fixes the instance, human principal, submission client, and
separate source/decision namespace and key IDs. IDs identify trust configuration;
they do not contain secret key bytes or filesystem paths.

A decision check obtains the current proposal review through the existing
neutral review entry. It binds the durable source, current proposal version and
content hash, complete visible review projection, configured human principal,
service instance, decision key, action and expiry. The submitting client is
recorded separately; submitting a source does not make that client a human
approver. A consumed or stale decision is refused. No operation silently maps
one identity to another.

Proposal versions preserve the existing Core convention: the initial version
is `0`, so valid versions are nonnegative integers. Boolean values do not count
as integers. Evidence times are positive integer UTC seconds, with source
occurrence no later than receipt, receipt no later than inspection, and human
decision issuance no later than inspection strictly before expiry.

The returned observation can become stale immediately after the read snapshot.
It is not a transferable capability or a promise of future posting. A future
posting service must revalidate all bindings and atomically link decision
consumption with its recoverable result inside its own guarded write unit of
work. This entry does not implement that transaction.

## Historical compatibility

New evidence uses independent versioned schema domains. Old Telegram source
contexts, OpenClaw delivery proofs, callback references, and D2 decisions retain
their original verification rules. They cannot be relabeled as new independent
source or decision evidence. Historical D2 records remain verifiable through
their existing owners, including `D2OriginalSourceVerifier`; no historical
record, signing identity, migration, or authority API is rewritten here.

The schema constants are `finance-application-source-v1` and
`finance-application-human-decision-v1`. `review_projection_sha256` hashes the
entire neutral review inside `finance-application-review-projection-v1` framing.
It uses canonical sorted JSON and refuses nonfinite numbers; it does not hash
only a reduced selection of displayed fields. Adapter signatures must
authenticate their own source versus decision domains and complete durable
material. Relabeling old signed bytes does not create valid new evidence.

This additive entry leaves the stable `finance-core-api-v1`, Bridge envelope,
package dependencies, and exact platform-dependency exception registry
unchanged. It does not claim that the existing legacy posting entry has become
platform independent. See [the existing review entry](application_review_v1.md)
for projection semantics and the remaining legacy dependency boundaries.

## Validation

Use synthetic signed adapter evidence in temporary SQLite databases. Exercise
durable close/reopen readback, source and human identity separation, wrong
schema/key/instance/source/proposal/version/content/projection/action/expiry,
consumed decisions, forged or missing evidence, and immutable observations.
Prove no new database changes or final facts, preserve caller transaction
ownership, and block platform imports in a cold interpreter. Keep the existing
Application review, dependency guard, source/human-action and D2 history/posting
regressions. Applicable Python quality checks and the exact-head hosted Python
and both Bridge systems remain required by
[the validation contract](release_validation_v1.md).
