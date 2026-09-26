# D3-4 authenticated capture discovery v1

`list_capture_recovery_candidates` is a read-only Bridge command for a
background worker that already has the current trusted Telegram actor,
account, conversation and binding. It also requires the staging workspace,
`after_job_id` (zero for a new sweep), and a `limit` from 1 to 100. The first
response freezes `through_job_id` at the greatest saved integer job ID. Pass
that value and the returned `after_job_id` into later pages of the same sweep.
Each candidate contains only the job public ID, integer job ID, original
Telegram message ID and saved D2 source identity hash. A candidate is a
locator, not authority to process, post, enqueue or send.

The query checks all four saved identity fields and rechecks the immutable
Telegram source for each returned job. It deliberately includes old jobs in
every new sweep, regardless of their current processing status. Later D2
acceptance, a D2b correction, or a missing result outbox row can make an old
job actionable. A worker must start periodic sweeps again at `after_job_id=0`;
advancing a cursor forever would miss those changes. Each candidate still
requires `get_capture_recovery` and, for a permitted local action, the
token-bound `resume_capture_recovery` command. Those commands independently
check the current business state and saved binding.

`get_capture_job_for_message` locates the original capture by positive
`telegram_message_id` under the same complete saved identity. It returns the
same locator or `null` when there is no matching original capture. This allows
an authenticated original-message status request even if the host lost its
immediate Core response. The locator is not proof that the host adopted the
capture: the host must separately verify its own durable adoption guard and
complete original `finance_ingress` object. Core stores that object's digest,
but not all of its original fields.

This slice adds no worker, provider call, send claim, or host receipt consumer.
The existing D2 posting review delivery authority remains the source for
actionable review cards. Informational notices can be derived from an
authenticated recovery view and its step token, but the Bridge must reread
that view immediately before asking a future host to enqueue a durable notice.
A queued notice may become outdated before transport dispatch; its text must
identify the observation time and direct the user to a fresh status query.
Financial result replies and review cards need their separate stronger
delivery authority and receipt contracts.
