# Public pytest timing baseline

The pytest planner uses measured per-file medians from at least three
successful first-attempt protected-main push runs of `validate.yml`. Public
source identity is GitHub repository ID `1375123906`; its numeric identity is
stable across owner/repository renaming and does not need an owner-name
exception to public source privacy checks. The retired private bootstrap
identity is no longer accepted for new baseline provenance.

The September 22 refresh uses runs `35510434048`, `35617917326` and
`35687835685`. Their API records, complete successful ten-job inventories,
artifact run/head bindings and downloaded ZIP digests were verified before
importing their complete aggregate timing JSON. The baseline retains each
run/artifact/commit identity and the SHA-256 of the extracted timing JSON;
this JSON digest is distinct from GitHub's digest of the downloaded ZIP.

Refresh with `plan_pytest_shards.py build-baseline`, supplying verified source
metadata and the retained `pytest-timing-v1` compatibility option. Do not
invent timings for new files or change source metadata to make an untrusted
run eligible. Review a refresh against its downloaded public evidence.

The planner enforces the 5% unknown-file limit, 30-day age limit, P95
fallback, deterministic LPT assignment and exact once-only current Git test
inventory. Run the production `plan --shard-count 5` command locally before
requesting full Hosted validation. A baseline regression uses a fixed sample
date. The production planner checks age against the current date and admits
the candidate's actual inventory.

The September 22 provenance above is retained as refresh history. The October 1, 2026
refresh uses the three newest successful first-attempt `validate.yml` runs from push
events on the public `main` branch. The run, job, and artifact records were checked; all
ten jobs completed successfully. Each unexpired aggregate ZIP matched GitHub’s reported
SHA-256 digest, and its extracted JSON was validated and hashed separately. The
refreshed run, artifact, commit, and extracted JSON identities are recorded in the
manifest. Downloaded artifacts and API evidence remain outside the repository.

The October 11, 2026 refresh uses successful protected-main push runs
`37774334753`, `37815412975` and `37853300607`, each on attempt 1. All ten jobs,
repository/workflow/head bindings, ancestor commits and downloaded aggregate
ZIP digests were checked before the original baseline builder imported the
separately hashed complete JSON. These sources cover 251 current test files;
new PR files still receive the unchanged P95 fallback and must satisfy the
same inventory and age limits.

PR07 run `38099583338`, attempt 1, is retained as failure evidence, not a
baseline source. Its fourth Python shard reached the 60-minute job limit
before producing a complete result. The refreshed trusted baseline still
underestimates the new independent-AI history and v2 modules, which have no
successful protected-main timing yet. For the explicitly approved recovery,
the pytest job limit is 90 minutes. Four single-process shards, all ten
required jobs, complete test inventory, source trust and acceptance remain
unchanged. The larger bound is not proof of completion; only a complete
successful exact-head run supplies authoritative validation.

As of October 11, 2026, the pytest job timeout is 120 minutes. This change is
limited to that job-level timeout; the four single-process shards, test
inventory, required jobs and acceptance remain unchanged. The timeout is an
upper bound, not an expected duration or proof of completion; only a complete
successful exact-head run supplies authoritative validation.

## October 12, 2026 baseline refresh and operating guidance

The refreshed manifest was built with the existing `build-baseline` command
from three verified, successful first-attempt protected-main push runs. Their
run and head identities are:

| Run | Head |
| --- | --- |
| `38111411851` | `c5e881141b6ca8a326de1f1382601d51521130ce` |
| `38130916170` | `ff1a36db4db01987a9dc61d8408a40447c23326b` |
| `38169383980` | `0a41c1566da7a843eceb3d28d4a8e4c3653a3ebb` |

The complete manifest contains all 258 current test files, 12,247 test cases
and 36,739 per-test timing samples. The current production plan reports zero
unknown files. The manifest's `unknown_file_weight_seconds` value of 178.068
seconds is the P95 fallback weight reserved for a future file without timing;
it is not an unknown-file count. The builder still takes each test's median
from the available source samples and sums those medians by file. The source
minimum, 30-day freshness limit, 5% unknown-file limit, P95 fallback, four
single-process shards, deterministic LPT assignment and exact once-only test
inventory remain unchanged.

Historical replay used the same 258-file inventory. The latest two runs had
identical 12,247-case inventories; their replayed longest-shard cumulative
test durations were 30.09% and 11.44% below the prior assignments. Across all
three runs, the replayed maximum-to-mean shard ratios were 1.0403, 1.0267 and
1.0738. The replays place the new longest shards at roughly 58–72 minutes of
cumulative test duration. These are forecasts from historical test timings,
not measurements of hosted job wall time or promised work reduction.

### Reading the timing summary

The timing summary is a human-readable view of the validated plan and timing
artifact. It does not change the stable planner or aggregator command line,
`pytest-timing-v1` JSON schema, or success and failure exit codes.

- Per-shard forecast duration is the sum of assigned file weights. Actual
  duration is the sum of measured test durations in a complete timing
  artifact. Counts identify how many files and test cases contributed.
- Duration deviation is actual minus forecast, with the percentage calculated
  against a positive forecast. A missing, null, failed, incomplete, or
  zero-denominator value is not evaluable; it must not be shown as zero or
  treated as a pass.
- The maximum-to-mean ratio is the longest shard's cumulative test duration
  divided by the arithmetic mean across all shards. Top-ten file and case
  rows show each measured contribution and its share of the total measured
  test duration across all shards.
- The total test time is the sum of shard test durations. Dividing it by
  the shard count gives the ideal balanced-shard lower bound on the longest
  shard's test work. Setup, scheduling, runner variation and resource
  contention are outside that lower bound.
- Unknown actual cost is measured from tests whose file has no baseline
  weight. The plan applies the P95 fallback weight to each such file. Keep the
  unknown-file count, measured unknown cost and fallback estimate distinct.

The following are review hints, not CI gates: maximum-to-mean above 1.20
suggests shard imbalance; actual total test time divided by forecast total
above 1.25 suggests that the overall forecast was low; and any individual
unknown file's actual cost divided by its positive fallback estimate above 2
suggests that the baseline deserves review. Unknown-file costs are not summed
and divided by one file's fallback for this hint. Missing or incomplete
evidence makes the affected hint not evaluable.

Prepare a manual baseline review only after the same warning appears in two
consecutive qualified samples. A qualified sample is a complete, successful,
first-attempt `validate.yml` protected-main push with the full required job
set and a valid aggregate timing artifact. The review may prepare a refresh
from three verified eligible sources. It does not automatically edit the
manifest, change workflow state, start a workflow, or rerun a job.

For a candidate PR and the resulting landed-main measurement, the target
maximum-to-mean ratio is at most 1.20, assessed separately from correctness
and complete test coverage. Preserve and evaluate a failed run or a performance
miss as observed; do not select a later, faster retry as the result. The
120-minute timeout remains an upper bound, not a duration target.

## November 10, 2026 inventory admission repair

PR run `42834478417`, attempt 1, stopped in inventory planning before any of
its four Python shards executed. The retained baseline covered 258 files:
protected main had 13 unknown files out of 271, while the candidate's two
additional files raised that to 15 out of 273, exceeding the unchanged 5%
limit. This is failed preparation evidence, not a completed test result.

The baseline is regenerated with the existing builder from successful
first-attempt protected-main push runs `41357073470`, `41262670970` and
`40055204728`. Each run's repository, workflow, ancestor commit, complete
successful job inventory and unexpired aggregate artifact binding were
verified. The downloaded ZIP matched GitHub's SHA-256; the extracted complete
JSON has its own recorded digest. The first two workflows require eleven
jobs, including candidate-package; the older workflow retains its original
ten-job contract.

The refreshed manifest covers 271 files, 12,872 tests and 38,345 timing
samples. The two new candidate files still receive the measured P95 fallback;
no timing is invented for them. The source minimum, 30-day freshness limit,
5% unknown-file limit, four shards, complete once-only Git inventory and LPT
assignment remain unchanged. This repair responds to an actual admission
failure, separate from the advisory performance-warning review above. A new
complete candidate run, including both Bridge platforms required by this
baseline path, must establish authoritative validation.

## November 12, 2026 measured baseline refresh and interrupted-run diagnostics

The refreshed manifest was built with the unchanged builder from the three
newest eligible successful first-attempt public `main` push runs of
`validate.yml`:

| Run | Head |
| --- | --- |
| `42938974435` | `20176eeefb1aff6ed23914988984178336bb914f` |
| `42976612835` | `149e587c4ac31f750afcc262d5b2f331446c3f75` |
| `43134286804` | `730d247617a37ae8e6dd1c5add62a553cd15692b` |

The run, full eleven-job, artifact identity and digest evidence was verified;
ZIP digests matched and complete extracted timing JSON was separately hashed.
The manifest records 277 measured files, 13,110 tests and 39,024 timing
samples. Its P95 fallback is 271.335 seconds per file. The current candidate
has 282 test files; five still have no eligible main timing and use the
unchanged fallback. No failed pull-request timing was imported.

The baseline review followed the existing advisory process. Two consecutive
qualified samples emitted `refresh_unknown_weights` for different files: run
`42976612835` measured `tests/test_lifecycle_recovery_material_v1.py` at
590.200 seconds against the then-current 237.810-second fallback (2.481813x),
and run `43134286804` measured
`tests/test_prepared_native_finalization_v1.py` at 597.743 seconds (2.513532x).
This supports manual preparation under the existing policy; it does not
change any threshold or CI gate.

Candidate run `43158199367`, attempt 1, failed after shard 0 reached the
existing 120-minute job limit. Its partial JUnit-based timing report was
uploaded with `junit_xml_not_found`; all four shard timing reports were
uploaded, and the incomplete shard report remains failure evidence. The shard
command now uses pytest's verbose case output (`-v`) in place of quiet dot
progress so an interrupted log identifies the active test case. The exact
Git test inventory, four single-process shards, JUnit path, exit-code
propagation, 120-minute timeout, required jobs and acceptance minima remain
unchanged.

Historical per-test-duration replays on the three eligible sources produce
new-plan maximum-to-mean ratios of 1.140827, 1.183618 and 1.126748. These
known-cost replays do not show an improvement over the old assignment in the
latest-three comparison and are not runner wall-time measurements or a
completion guarantee. The five new candidate test files still lack eligible Linux
main timing, so the refresh alone is not evidence that the candidate will
finish within two hours. A complete successful exact-head run remains the
validation authority.

## November 14, 2026 baseline refresh after the C1 test grouping

The refreshed manifest was built once with the unchanged original builder
from the three newest eligible complete first-attempt protected-main push
runs of `validate.yml`. Their run and head identities appear below.

| Run | Head |
| --- | --- |
| `42976612835` | `149e587c4ac31f750afcc262d5b2f331446c3f75` |
| `43134286804` | `730d247617a37ae8e6dd1c5add62a553cd15692b` |
| `43221185644` | `ae9f56079085c0a5a41c5913ae0379bba8b82a2c` |

All three sources are successful first-attempt public-main pushes with the
complete required job set and complete aggregate timing reports. Their source
identities, downloaded artifact ZIPs and extracted JSON hashes were verified
before building. The manifest covers 282 measured files, 13,419 test cases
and 39,489 timing samples. Its P95 fallback remains an estimate for files
without eligible measurements and is 319.474 seconds per file.

Two successive qualified main samples meet the existing two-sample basis for
manual review under the `refresh_unknown_weights` advisory. Run `43134286804`
measured `tests/test_prepared_native_finalization_v1.py` at 597.743 seconds
against the 237.810-second fallback in effect for that sample. Run
`43221185644` measured `tests/test_prepared_envelope_faults_v1.py` at
1,434.163 seconds against the 271.335-second fallback in effect for that
sample. The advisory does not change the baseline thresholds or CI gate.

The post-grouping tracked test inventory contains 286 files. The production
planner assigns every file once across the unchanged four shards. The manifest
has measured weights for 282 files. Four C1 files remain unknown, giving
98.6014% known-file coverage. Those files are listed below.

- `tests/test_lifecycle_operation_lookup_v1.py`
- `tests/test_single_host_runtime_faults_v1.py`
- `tests/test_single_host_runtime_v1.py`
- `tests/test_single_host_runtime_prepared_scope_v1.py`

The new prepared-scope module groups 18 existing prepared-operation
regression cases. The runtime module retains its other 84 cases. Neither module
has eligible protected-main timing and both receive the unchanged P95 fallback.
No PR timing was used to assign either file a weight.

The current production plan has digest
`sha256:78be2f870a4c9cb58674cb4e30167a9de66222e13b24ec5b811fa63faabc432d`.
Its shard file counts are 69, 70, 69 and 78. Forecasts sum per-file weights
and are 6,085.700, 6,085.700, 6,085.701 and 6,085.700 seconds. Hosted
wall-time remains to be measured.

The complete eligible main reports were regrouped using this exact
post-grouping plan. The table reports shard 0/1/2/3 testcase-seconds and the
maximum-to-mean ratio.

| Source run | Regrouped shard testcase-seconds | Maximum-to-mean |
| --- | --- | ---: |
| `43221185644` | 6,190.402 / 6,497.753 / 6,161.198 / 6,755.326 | 1.055 |
| `43134286804` | 4,321.989 / 3,908.489 / 3,577.717 / 4,877.992 | 1.169 |
| `42976612835` | 6,255.868 / 5,198.885 / 4,365.883 / 4,901.803 | 1.208 |

These historical groupings sum complete eligible testcase durations under the
current file inventory. The `42976612835` replay has a 1.208 maximum-to-mean
ratio, above the existing 1.20 advisory target. This is historical assignment
evidence. Candidate wall-time and completion remain unverified.

PR 73 run `43436976339`, attempt 1, remains failure evidence. Shard 1 had one
pytest failure and shard 3 hit the existing 120-minute job limit. Its last
logged completed test was
`tests/test_telegram_attachment_acquisition.py::TestByteHeaderAndTimeoutLimits::test_malformed_content_length_is_rejected[x]`
at `2026-11-13T15:50:02.0049010Z`, followed by cancellation at
`2026-11-13T15:50:03.1776294Z`. Shard 3 has no complete JUnit timing report
or final case count. Failed-PR timing remains diagnostic evidence and was not
imported. The four shards, full inventory, job timeout, required jobs and
acceptance criteria remain unchanged. Authoritative acceptance requires a
complete successful exact-head run.

## November 18, 2026 qualified-main refresh after the main timeout

Main push run `44164836792` at `74943c34fa38f40663aeaaa4cf84987cec25ca0d`
reached the existing 120-minute pytest job limit. The run did not pass:
eight jobs succeeded, one pytest shard was cancelled, and `pytest-report` and
`validate` failed because that shard had no complete JUnit report. Its partial
log showed 4,339 collected cases and 4,307 terminal outcomes (4,108 passed,
199 skipped, zero logged failures or errors); 32 cases had no terminal outcome.
All 32 are in `tests/test_telegram_attachment_evidence.py`. Missing terminal
outcomes may include work already in progress, and the partial log is not a
complete test result.

The prior manifest timestamp, `2026-11-17T09:27:49Z`, was still within the
existing 30-day limit. This refresh responds to the observed timeout and the
newer qualified-main timing distribution; it does not treat the prior baseline
as expired. The existing two-sample `refresh_unknown_weights` advisory remains
manual and unchanged. This incident does not add an automatic timeout trigger.

The manifest was rebuilt with the unchanged production `build-baseline` CLI
from three successful first-attempt protected-main push runs of
`validate.yml`. Each had a complete aggregate timing artifact and all 11 jobs
successful. The source identity is GitHub repository ID `1375123906`; the
artifact ZIP hash and extracted JSON hash are recorded separately:

- Run `44006821314`, head `699ab8e61f3d1c2dd0b8435f6e22f3fe9009d32c`,
  artifact `14297479848`; ZIP SHA-256
  `348bce425ef0f85ab401c2860887b967686e6cd7f63b221d948117eb57500f6d`;
  JSON SHA-256
  `efe7c6282ea5a3613e87ca83da6b4f4c1c2ce6e23e5e21f5e4c5ed95878ead2c`.
- Run `44055796273`, head `0c2aacc7fb9e7a43bf36841ab10bf2784caea754`,
  artifact `14323070228`; ZIP SHA-256
  `ae71cf846b190158314bacd50938af19507ca7b39e4b1aef48fb41c15d02e3f3`;
  JSON SHA-256
  `1bb7184b0f60e8683fccddf34a511300a657143e83c5a7526fcf2fb048c8bbf2`.
- Run `44137500031`, head `f358a6b11be9576cb6c7f4b64fb7a7b3b7e2d243`,
  artifact `14361932616`; ZIP SHA-256
  `aa11d7c649793e0b5a9884bbeba70f49f38564d5623edc22f76283fe53ff4697`;
  JSON SHA-256
  `5bb8ab66f411e6a921347bbb4334ef4f452d53fa693c467cd8250231b2d92fc9`.

The generated manifest timestamp is `2026-11-17T17:29:13Z`. It contains 291
measured files, 13,795 test cases and 41,145 timing samples. The same P95
calculation yields a fallback of 234.378 seconds per file with the refreshed
measurements, up from 231.880 seconds. The production plan has digest
`sha256:96d61cd18ece059984b68d7d5b68e17b04384fc828f3be6c45abe064795010a8`.
It assigns all 292 tracked test files exactly once across four non-empty
single-process shards (69, 70, 83 and 70 files). One new file,
`tests/test_daily_movements_lifecycle_v1.py`, uses the existing fallback;
known-file coverage is 99.6575%. The 5% unknown-file limit and 30-day age
limit remain unchanged. The manifest SHA-256 is
`c8f98622ec6cb4034905d40026928b85bb614acf3aeebd3071110d112150c263`.

A complete same-tree PR77 run, `44143242310`, is diagnostic comparison only
and was not imported as a source or used to assign weights. Replaying its
complete testcase timings on the old and refreshed plans changes the longest
shard from 115.706 to 97.635 minutes and max-to-mean from 1.3463 to 1.1360.
These are testcase-time estimates, not hosted wall-time or acceptance proof.
For the three complete shards on main versus that same-tree PR run, testcase
times ranged from 0.688x to 1.377x; the cause of this run-to-run variation is
not established, and no replay guarantees completion on a future runner.

The 120-minute job limit, four shards, 11-job workflow, complete inventory,
reporting and failure propagation are unchanged. Only the trusted timing
manifest and this evidence record were refreshed. A complete successful
exact-head hosted run remains the acceptance authority.

## November 18, 2026 five-shard capacity repair

PR 78 run `44191676341`, attempt 1, tested the preceding four-shard
refresh. Shard 0 wrote complete JUnit timing before the runner marked it
cancelled at the 120-minute job limit. The aggregate recorded 13,499
passed and 362 skipped tests, with no failures or errors. The timing
report job succeeded, and final validation correctly rejected the
cancelled pytest matrix. The failed run is diagnostic evidence.
The baseline retains the three protected-main sources recorded above.

The current workflow assigns the same inventory to five single-process
shards with 120 minutes per job. Both workflow planning steps select five,
and aggregation requires the complete five-report plan. The planner and
aggregator continue to accept the historical two- and four-shard formats.
The aggregate command exits unsuccessfully for an incomplete or errored
report and preserves the actual pytest exit code and statistics in the JSON.
The production plan at this change assigns all 292 tracked test files once
in groups of 54, 56, 56, 69 and 57, with digest
`sha256:313f6c8769411e38a521faa8bb1f574b8866eb820fb95e507441e5476d8569de`.

Replaying the five assignments with complete timings from the failed run
sets the longest shard at 83.021 cumulative test minutes, compared with
119.300 in the actual four-shard assignment. Replaying complete timings
from PR 77 sets it at 80.257 minutes. These replays measure test work.
Hosted elapsed time also includes setup, scheduling and runner variance.
The repair requires successful exact-head Hosted validation and a
successful protected-main push with the current graph.
