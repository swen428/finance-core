# Pytest shard timing controls

`pytest-shard-weights-v2.json` is the fail-closed weight baseline used by the
four Linux pytest jobs. The initial baseline contains measurements imported
from the pre-split private repository, but its repository, workflow, run, and
artifact identifiers are deliberately redacted. Test names and durations are
synthetic-suite metadata only.

Every validation run uploads per-shard timing reports and one aggregate
`pytest-timing-v1` artifact. Before the committed baseline reaches 30 days old,
an owner must download three successful aggregate artifacts and use
`plan_pytest_shards.py build-baseline` to create a reviewed replacement. The
planner also fails closed when unknown test files exceed five percent, so new
tests cannot silently accumulate on one default-weight shard.

Do not weaken either threshold to make CI pass. Refresh the evidence instead.
