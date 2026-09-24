# D3 local capture processing and recovery v1

Migration `053_d3_capture_processing.sql` adds nullable, unique stage identity
bindings to the existing capture job. A receipt job binds one deterministic OCR
extraction ID, parser proposal ID, and OCR-to-proposal link ID before OCR starts.
A text job reuses the proposal created by the capture transaction. The job
never grants confirmation, conversion, or finalization authority.

`process_capture_job` accepts an existing job public ID and uses the local OCR
engine configured for the staging workspace. A worker first claims the job
with `BEGIN IMMEDIATE`, a bounded lease, and a new monotonically increasing
epoch. Renewal requires the exact owner and epoch and a still-live lease.
Expired claims can be reclaimed; an old worker cannot save after reclamation.
The request can be retried after a lost response. `get_status` exposes the
same job and stable stage IDs without starting processing.

OCR reads and runs outside a SQLite write lock. The owning OCR and proposal
services each start their own `BEGIN IMMEDIATE` saving transaction and call
the processor's lease check before commit. A stale or expired epoch rolls
back that stage's write. The proposal transaction also changes the job to
`awaiting_user` and clears the lease, so proposal evidence and job completion
cannot diverge. A crash after OCR commit leaves its evidence and stage ID;
the next worker verifies and reuses them. Failure leaves a durable
`needs_attention` job when the current lease is still valid.

The processor reads existing AI attempt, invocation claim, and result records.
A claim without a result projects `outcome_unknown`; it never starts or
retries a provider call. Existing AI services remain the invocation and
result authority. The processor does not send Telegram replies or create an
outbox. It does not create final financial facts, transactions, or edits.

Acceptance uses temporary staging databases and synthetic receipt bytes:
lease contention and renewal, expired claim fencing at OCR and proposal save,
crash after OCR commit, stable replay, existing AI claim with unknown result,
and absence of final transactions.
