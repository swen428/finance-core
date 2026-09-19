# Finance Core working rules

## Scope and authority

This repository owns the generic deterministic engine, immutable migration
history, public API, synthetic tests and generic Bridge package. Start each
task with its approved objective, exact protected-main baseline, affected
contracts and observable acceptance criteria. A consumer's private roadmap
may supply the task; retain its private context outside this repository and
record only the approved public scope here.

Python owns monetary calculations. Parser and AI outputs are proposals;
confirmation records a decision, and only guarded finalization creates facts.
Preserve source evidence, audit history, transaction boundaries and replay
protection. Test only with synthetic inputs and temporary databases. Keep real
data, credentials, personal configuration, operational evidence and private
repository history outside Git.

## Changes and validation

Create a fresh `codex/<task>` branch from current protected `main`. Inspect
the diff before publishing. Never push directly to `main`, use administrator
bypass, rewrite historical migrations, or replace an existing release/tag.
Append new migrations with explicit ledger/API compatibility evidence.

Before editing, trace affected callers, tests, resources, packaging and
consumer contracts. Financial, persistence, schema, runtime, security,
dependency and workflow-governance changes are HIGH-RISK. Apply the highest
risk of the actual diff. Run focused tests and applicable Python quality
checks before final review; inspect the declared Python and Node versions.
Use clean installation/Bridge proof for package or dependency changes.

## Review and delivery

Freeze the exact base/head and resolve all blocking findings before delivery.
Financial, persistence or runtime changes, changes to minimum acceptance or
validation selection, and mixed scope require three independent read-only
specialists: financial/domain, persistence/audit, assurance/security. Pure
delivery governance with unchanged financial acceptance requires two:
CI/delivery correctness and security/evidence. Verify findings independently;
an unavailable scope leaves review incomplete. Changed base/head requires
updated exact-diff coverage. Reviewers do not duplicate the full CI suite.

Use bounded execution for mechanical work, substantive financial/security
judgment for specialist review, and architecture review for cross-repository
design or conflicting findings. A model-tier change cannot replace tests,
independent reviews or approval. Keep development routing separate from the
product's runtime models and providers.

Before merging or releasing, follow
[the release validation contract](docs/development/release_validation_v1.md).
Obtain explicit repository-owner approval for the final high-risk candidate;
submit one protected squash attempt and resolve uncertain results read-only.
Record the landed parent/tree and required validation. Hosted CI success,
merge approval, release publication and runtime activation are separate gates.
Existing task grants do not authorize a new public release or runtime use.

Consumers upgrade only after a verified immutable version/commit/artifact/API/
migration-ledger identity exists. They must admit that identity with trusted
controls before changing the active dependency. Preserve old release identities
for audit and rollback; a consumer upgrade is a separate change.
