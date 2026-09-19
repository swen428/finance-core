# Public validation and release contract

## Candidate checks

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
