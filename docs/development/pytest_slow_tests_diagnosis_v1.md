# Diagnosis of three existing slow pytest cases

This note records a one-time, read-only profile of three existing cases. Each
case ran once with synthetic inputs and a temporary or in-memory database.
The run cap was ten minutes per case and thirty minutes total; all completed
well within those caps. The process wall times below include local profiling
overhead. The runs used macOS, Python 3.12.13, pytest 9.0.3 and `cProfile`.
They are not Linux benchmarks, hosted CI measurements, or evidence that a
particular commit caused a regression.

| Existing case | Call phase | Process wall time | Result |
| --- | ---: | ---: | --- |
| [Mixed human/AI history case](../../tests/test_independent_ai_history_v1.py#L963) | 112.473 s | 113.562 s | Passed once |
| [Authenticated replacement case](../../tests/test_independent_ai_v2.py#L2473) | 14.289 s | 15.416 s | Passed once |
| [Exact edit posting case (`True` variant)](../../tests/test_independent_ai_invocation_v1.py#L1861) | 20.633 s | 21.436 s | Passed once |

Combined process wall time was 150.414 seconds. All three exercised the real
Core validation and posting or recovery paths, with synthetic provider
responses and isolated databases. The findings below describe these runs;
they do not estimate the effect of a change or a production workload.

## Confirmed cost centers

The history case ended with five parser outputs, two amendment records, two AI
publications, 28 field-evidence rows and ten audit events. Its profile counted
2,978,150 SQLite `execute` calls, with 26.218 seconds of profiler self time in
those calls. This is a count of SQL calls, not millions of stored facts.

The profile and callers show repeated reconstruction of lineage, seal and
source-proof evidence in the mixed history flow. For example,
[`verified_source_revision`](../../finance_core/parser_proposals/independent_ai_revision.py#L107)
and [`verify_node_ownership`](../../finance_core/parser_proposals/independent_ai_lineage.py#L766)
re-enter validation of inherited field markers; the marker check
[`_verify_inherited_field_marker`](../../finance_core/parser_proposals/independent_ai_lineage.py#L925)
revalidates the associated amendment record and its source evidence. The
history profile observed 9,625 ownership-verification calls and 47,980
publication-creation-record verifications. These counts establish repeated
work in this case, but do not establish exponential complexity or identify a
regression-causing commit.

The other two cases also spend most of their call phase in genuine posting,
recovery and read-back validation. Their profiles counted 332,279 and 430,825
SQLite `execute` calls respectively. The corresponding completion paths
include writes and checks of the resulting transaction, receipt and
authoritative snapshot; their full duration must not be labeled disposable
duplicate work.

Receipt preparation and migration were small in these profiles. The
`_prepare_receipt_subject` helper took about one second per case, including
about one second for migration. Migration time is included in the helper
total, so those figures must not be added. Mock provider dispatch took about
0.0015 seconds per case. Neither area explains the history case's dominant
cost.

## Follow-up boundaries

| Finding | Evidence | Possible follow-up | Isolation and acceptance impact |
| --- | --- | --- | --- |
| The mixed history flow repeatedly rebuilds lineage, seal and source proofs. | Small final evidence counts alongside 2.98 million SQLite calls and repeated ownership, publication and amendment verification. | In a separately scoped performance change, study proof reuse within one transaction or query batching. | Preserve fail-closed behavior and prove equivalent tamper detection, replay, rollback, provenance and final read-back behavior. No process-wide cached payload may stand in for evidence. |
| The posting and recovery cases perform real validation work. | Their profiles show costly completion paths and each produces one transaction, receipt and authoritative snapshot. | Optimize only after profiling a concrete repeated sub-operation and showing a safe boundary. | Keep every existing business assertion, temporary database isolation, write boundary and recovery check. Do not remove checks based on cumulative profile time alone. |
| Test preparation and migration contribute a small amount. | Preparation, including migration, is about one second per case. | A separately tested copy of a migrated template could be explored as a harness improvement. | Preserve per-case database and workspace isolation, migration ledger, schema identity and synthetic custody. This is a small possible saving and does not address the main bottleneck. |
| Mock dispatch is negligible in these runs. | About 0.0015 seconds per case. | No dispatch optimization is indicated by this evidence. | Keep synthetic provider behavior explicit; this profile does not measure a real provider. |

Same-transaction proof reuse or query batching is the strongest next direction
to investigate, in a separate performance change with an explicit scope. Any
proposal must retain the existing fail-closed checks and demonstrate equivalent
behavior under tampering, replay and rollback. This diagnosis does not
authorize cached payloads as evidence, deleted checks, shared mutable business
databases, or changes to current business code.

Unconfirmed questions include Linux runner and storage effects, runtime
variation across repeated samples, the commit that may have introduced a
historical slowdown, production history depth, and the performance gain of
query or proof-reuse changes. Raw profiles and run outputs are retained
outside this repository; this note contains only a summary and links to the
public tests and source paths.
