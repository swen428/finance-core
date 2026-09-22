# Public validation and release contract

## Candidate checks

Finish focused local validation, freeze the current protected-main base and
candidate head, and complete independent exact-diff reviews before publishing
the final candidate. Every validation lane explicitly checks out that head;
it must include the frozen base. GitHub's default PR merge commit remains an
event/workflow identity, not the tested source identity. See the official
[PR event semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).

PR validation runs Python quality checks, the complete sharded Python inventory
and its aggregated inventory/timing proof. The Bridge runs when its inputs or
the CI/build/dependency controls change. A PR with no Bridge requirement may
have a skipped Bridge job; a required Bridge may never be skipped. All Actions
remain pinned and all fork execution uses read-only permissions without secrets
or persisted checkout credentials.

The required `validate` job depends on classification, quality, Python tests,
Bridge and the aggregate report. A failed, cancelled or missing required result
blocks validation. Reporting is required because downstream consumers verify
its job identity and success as part of their release evidence.

The final `validate` job also requires matching classification/checkout
identities and uploads `candidate-validation-v1-<run>-<attempt>`. The record
contains repository, PR, event/run/attempt, frozen base/head, actual tested
commit/tree, workflow ref/commit/content digest, Bridge scope and all grouped
required results. Every source/build/provenance check uses the same candidate
head. Changes to the Application review or its compatibility export packages
also require both Bridge systems.

One successful attempt-1 complete PR run for that frozen candidate is the
normal authoritative V2. Immediately before protected merge, use the reviewed
verification tool with the reviewed identities, for example:

```text
python scripts/verify_candidate_validation.py --repository OWNER/REPO \
  --pr NUMBER --run-id RUN --base REVIEWED_BASE_SHA --head REVIEWED_HEAD_SHA \
  --output-dir NEW_EVIDENCE_DIRECTORY
```

This read-only check fetches the current PR/main, immutable commit/workflow
records, exact run/attempt/job inventory and that run's proof artifact. It
verifies the downloaded artifact digest, candidate and synthetic-merge tree
binding, workflow bytes, applicable Bridge scope and every required job result.
It rejects changed base/head, missing/failed/cancelled/duplicate jobs, stale or
foreign proof, incomplete changed-file inventories and workflow mismatches.
The artifact alone is insufficient. Its receipt is a point-in-time observation;
if delivery waits, refresh it before merge. Strict branch protection and an
exact-head merge submission remain required to close the final drift window.

Do not routinely dispatch a second unchanged full run after that verified PR
run. Manual full validation remains available for an explicitly authorized
recovery or diagnostic plan; it is not normal PR evidence and cannot silently
replace a failed or stale proof. A failure preserves its evidence and requires
the scoped repair/review/validation authority for a new attempt. Changing a
branch or candidate does not erase the failure history. This contract's
introducing change must meet its pre-existing full-validation requirements;
it cannot use the proposed optimization to lower its own acceptance.

## Release proof

Every push to protected `main` runs both Ubuntu and macOS Bridge jobs, including
pushes that only change Python, migration, dependency or documentation paths.
This produces complete evidence for the exact landed release commit. A manual
run also exercises the complete matrix but is diagnostic; it does not replace
the push-event evidence accepted by consumers.

Before publishing a new release, require a successful attempt-1 `push` run on
the exact protected-main commit, with this exact successful job inventory:

- `classify`, `quality`, `pytest (0)` through `pytest (3)`;
- `bridge (ubuntu-latest)`, `bridge (macos-15)`;
- `pytest-report`, `validate`.

Preserve its run/job identities and workflow digest. PR checks alone are not
release evidence. A failed or cancelled authoritative run requires stopping and
recording the cause, followed by a newly approved candidate/validation plan;
do not manufacture a qualifying result by rerunning an old attempt.

Build and inspect the wheel, source distribution and Bridge from the exact
clean commit. Bind the annotated tag, commit, API version, complete migration
ledger and every artifact's name/size/SHA-256 in the release manifest. Obtain
explicit approval for the new release identity before publication. Preserve all
published versions and their tags/assets unchanged.

## Protected merge approval

Independent specialist reviews and explicit owner approval bind the exact
candidate. The live repository protection must also be satisfied without
administrator bypass. If required approvals cannot be supplied by eligible
non-author reviewers, stop with the concrete candidate and propose a separately
approved protection/approval design; do not silently reduce the requirement.
Check the approved settings against their live readback before each delivery.

## Consumer handoff

Provide the published identity and complete push validation evidence to the
consumer's separate dependency-upgrade task. Its trusted allowlist must admit
the exact new lock before a candidate consumes it. Verify independent package
installation and the fixed Python/Bridge combination; mismatched release,
artifact, API or ledger identity must fail before installation or use.
Publication and a consumer dependency upgrade do not authorize live data,
provider access, runtime installation or activation.
