# Public pytest timing baseline

The four-shard planner uses measured per-file medians from at least three
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

The 5% unknown-file limit, 30-day age limit, P95 fallback, deterministic LPT
assignment, four shards and exact once-only current Git test inventory remain
unchanged. Run the production `plan` command locally before requesting full
hosted validation; focused pytest alone does not prove that the hosted planner
can admit the candidate's current inventory. A passing baseline regression
uses a fixed sample date; the production planner still enforces current age.

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
  divided by the arithmetic mean across the four shards. Top-ten file and case
  rows show each measured contribution and its share of the total measured
  test duration across all four shards.
- The total test time is the sum of shard test durations. Dividing it by four
  gives the ideal balanced-shard lower bound on the longest shard's test work;
  it is not a wall-time guarantee. Setup, scheduling, runner variation and
  resource contention are outside that lower bound.
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
