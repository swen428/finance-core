# D3 durable capture jobs v1

Migration `052_d3_capture_jobs.sql` adds a resumable job for each Telegram raw
intake. It does not alter existing intake keys, attachment evidence, parser
proposals, confirmation, or finalization authority. A capture job is source
evidence and processing state, never an economic event.

The `capture` command now returns `capture_job` with a stable `public_id`
(`fcj_` plus the first 40 hex characters of SHA-256 over
`finance-capture-job-v1\0` followed by the intake public ID). A replay of the
same chat/message and identical content returns that job. Changed raw content,
original image, or ingress identity fails with an idempotency conflict. The
`get_status` command accepts either the existing `intake_public_id` or a new
`job_public_id` and includes the durable `capture_job` in its response.
For a receipt job, `get_status` also returns `capture_attachment_integrity`:
`verified` only after reopening the current original and matching its immutable
size, signature and SHA-256; `missing` when that proof fails. Other jobs return
`null`. A lost-response recovery must require `verified` before reporting
original-image custody to the host. The job row alone is not that proof.

New capture clients may supply `finance_ingress` with exactly these fields:
`channel: "telegram"`, `accountId`, integer `updateId`, integer `chatId`,
integer `messageId`, integer `senderId`, `bindingId`, and lowercase 64-character
`payloadSha256`. Receipt images additionally require lowercase
`attachmentSha256`. The four existing authenticated Telegram source-context
arguments must accompany it. Core checks account, binding, chat, message,
sender, and available update ID against the validated capture source; for
images it checks `attachmentSha256` against the published original. It stores
`ingress_identity_digest`, SHA-256 of UTF-8 JSON with lexicographically sorted
keys, no insignificant spaces, and no ASCII escaping. The Bridge must
independently compare that digest before telling the host that it adopted an
event. Legacy capture without `finance_ingress` remains possible for existing
clients and has a null digest; a null digest must never be used as an adoption
receipt.

For text, raw intake, parser proposal, authenticated source context, and job
share one SQLite transaction. For receipt images, raw intake may already exist
from an earlier interrupted attempt. The immutable original file is published
first, then its attachment source and job share one SQLite transaction. A
failed source/job transaction leaves the prior raw intake and possibly an
orphan content-addressed file, but returns no successful capture; a replay
verifies and reuses the original. Only a successful result with a non-null
matching ingress digest and, for an image, a linked original image, may be
considered for host adoption.
Capture checks an on-disk SQLite journal and sets this connection to
`synchronous=FULL` before any capture write; if the setting cannot be verified,
the command fails without an adoption receipt.

The job starts as `captured`, `ai_status=not_started`, `reply_status=pending`.
Processing leases, AI outcome transitions, reply delivery and canonical
result reconstruction are separate later changes. This boundary performs no
AI call, OCR, confirmation, finalization, or Telegram send.
