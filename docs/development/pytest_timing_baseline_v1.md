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
