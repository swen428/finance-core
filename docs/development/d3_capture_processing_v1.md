# D3 local capture processing and recovery v1

Migration `053_d3_capture_processing.sql` adds nullable, unique stage identity
bindings to the existing capture job. A receipt job binds one deterministic OCR
extraction ID, parser proposal ID, and OCR-to-proposal link ID before OCR starts.
A text job reuses the proposal created by the capture transaction. The job
never grants confirmation, conversion, or finalization authority.

The local OCR/proposal processor remains in `finance_core.application.capture_processing`.
The D3 review, committed-result recovery, and reply-outbox services live in
`finance_core.openclaw_staging_bridge` because they depend on authenticated
Telegram context and D2/D2b platform authority. The former `application`
module paths are not public compatibility aliases.
The known-job query and one-step continuation contract is in
[D3-3 processing and result recovery](d3_processing_result_recovery_v1.md).

`process_capture_job` accepts an existing job public ID and uses the local OCR
engine configured for the staging workspace. A worker first claims the job
with `BEGIN IMMEDIATE`, a bounded lease, and a new monotonically increasing
epoch. Renewal requires the exact owner and epoch and a still-live lease.
Expired claims can be reclaimed; an old worker cannot save after reclamation.
The request can be retried after a lost response. `get_status` exposes the
same job and stable stage IDs without starting processing.

The production OCR adapter uses the existing 30-second extraction limit.
`process_capture_job` gives its claimed worker a 90-second lease to allow OCR,
original verification and the fenced save. The Core CLI's default 30-second
deadline is cooperative at phase boundaries and does not interrupt a running
OCR process; the OCR adapter enforces its own deadline. The Bridge subprocess
runner permits at most 105 seconds, but the later D3 Bridge worker must use a
shorter per-call timeout (at most 75 seconds), observe the Core retry time,
and let the lease expire before reclaiming a killed or unresponsive worker.
That worker scheduling and runtime timeout proof remain a later integration
gate; this Core change does not start a background worker.

OCR reads and runs outside a SQLite write lock. The owning OCR and proposal
services each start their own `BEGIN IMMEDIATE` saving transaction and call
the processor's lease check before commit. A stale or expired epoch rolls
back that stage's write. The proposal transaction keeps the job in
`processing` and clears the lease. Only after a D2 initial review and its card
are durably committed and their source/proposal binding is verified does
`ensure_capture_review` advance the job to `awaiting_user`. A crash between
those commits reuses the same review key and card. A crash after OCR commit
leaves its evidence and stage ID; the next worker verifies and reuses them.
Permanent failure leaves a durable
`needs_attention` job when the current lease is still valid.

An `OcrDeadlineExceededError` is a bounded local timeout, not proof that the
original is corrupt. The live worker atomically releases its lease and records
`ocr_timeout_retry_pending`, `ocr_retry_count` and
`ocr_retry_not_before_ms` on the same `processing` job. Claims before the
not-before time are refused; two retries follow delays of 10 and 30 seconds.
The third timeout becomes `needs_attention/ocr_timeout_exhausted`. A stale
worker cannot release another worker's lease or save late OCR evidence.
Original-image integrity, configuration and permanent OCR failures continue
to require attention. Recovery reuses the same source, OCR, proposal and link
identities; it never creates an additional economic event.

D2 initial cards have an immutable one-hour expiry. On replay, a card with 60
seconds or less remaining is no longer offered for delivery. Under a write
transaction, an unaccepted job becomes `needs_attention` with
`last_error=review_expired`; its original source and the expired card remain
queryable. Replays return that same state and create no new review. A durable
D2 acceptance is checked before this transition, and an already committed
result remains under the existing D2 result locator and reply outbox recovery.
This path neither extends an expired card nor issues a new action key.

The processor reads existing AI attempt, invocation claim, and result records.
A claim without a result projects `outcome_unknown`; it never starts or
retries a provider call. Existing AI services remain the invocation and
result authority. The processor does not send Telegram replies or create an
outbox. Migration 054's separate outbox verifies a committed result and
records `outcome_unknown` with a fresh nonce under `synchronous=FULL` before a
future sender may cross the transport boundary. It does not create final
financial facts, transactions, or edits. A job summary of `awaiting_user`
after an accepted but unfinished D2 posting is insufficient on its own: the
later Bridge recovery command must query the D2 posting status and result
locator before telling the user to confirm again.

## D3-2 processing test boundaries

These cases exercise the local processor in temporary staging databases. They
are not evidence that the separate D3-3 recovery command paths pass.

| Boundary | Checked-in tests |
| --- | --- |
| Claim renewal, expired-claim reclamation, and stale-worker fencing during OCR/proposal save | `tests/test_d3_capture_processing_v1.py::test_claim_renew_and_expired_reclaim_fence_old_worker`, `::test_worker_stolen_during_ocr_cannot_save_evidence`, `::test_stolen_after_ocr_cannot_save_proposal` |
| Reuse stage identities after OCR commit and preserve existing proposals | `::test_crash_after_ocr_replays_stage_identity`, `::test_text_processing_replays_existing_proposal_without_finalization` |
| Local OCR timeout schedule, stable retry IDs, exhaustion, and stale timeout fencing | `::test_ocr_timeout_defers_then_reuses_same_job_and_stage_ids`, `::test_three_ocr_timeouts_stop_without_spin_or_new_economic_event`, `::test_stale_ocr_timeout_cannot_release_successor_claim` |
| Existing AI claim without result | `::test_existing_ai_claim_without_result_is_unknown_and_never_reclaimed` |
| Bridge processor does not create a final transaction | `::test_bridge_process_command_replays_finished_local_job`, `::test_bridge_receipt_command_uses_local_engine_only` |

The D3-3 recovery query and token-bound resume, frozen D1/guided replay, D1/AI
child review, D2b policy binding and missing-outbox recovery boundaries are
tracked separately in
[`d3_processing_result_recovery_v1.md`](d3_processing_result_recovery_v1.md).
