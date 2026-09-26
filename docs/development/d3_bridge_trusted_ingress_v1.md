# D3-4 Bridge trusted ingress v1

The pinned OpenClaw host supplies `event.financeIngress` only for a trusted,
private Finance Telegram binding. The public Bridge checks that identity against
the bound event and context. A malformed or missing identity cannot produce a
custody receipt.

The Bridge does not reconstruct `payloadSha256` from Telegram's raw update;
that value and its binding to the original message are a fixed-host trust
contract. This slice proves that the same host-provided identity is saved by
Core and compared before a receipt. Without `financeIngress`, the existing
legacy controller remains registered. The fixed host refuses ordinary Finance
adoption without a matching receipt, so this source change alone is not a
runtime activation or a safety claim for an older host.

The pinned host routes `/finance` through its existing command/result authority
without `financeIngress`; it is outside this raw-message custody path. If the
host supplies a trusted slash message through this path, the Bridge still sends
it to Core `capture_interaction`, which freezes its control route in the capture
transaction.

Text, including D2 control text, uses Core `capture_interaction` with the exact
host identity and authenticated Telegram source context. JPEG and PNG use Core
`capture` with the same identity plus the hash of the original bytes passed
through the pinned handoff descriptor. These commands create or replay a durable
raw intake and capture job. They do not run OCR, AI, finalization, or Telegram
send. Later processing resumes from the Core job.

Core currently refuses a photo whose caption resembles a control message (for
example `完成`). The Bridge preserves that caption and returns no adoption. The
pinned host retains the original in its Finance spool, records a queryable
quarantine, and attempts a `needs_attention` notice. This is a visible recovery
case, not a successful Core capture; a future Core change must define the
handling if such photos are required to be captured automatically.

The Bridge returns `finance-ingress-adoption-v1` only after `get_status` confirms
the expected intake and deterministic job ID, the independently computed ingress
identity digest, and the capture kind. For a photo, Core must also reopen and
verify the linked original and report `capture_attachment_integrity=verified`.
This read can recover a lost capture response after a committed write. The
Bridge first calls `get_capture_job_for_message` within the authenticated
binding. An already captured message is read and verified without downloading
or writing the image again. Text intake IDs may be UUIDs, while receipt IDs are
derived from the capture key; the Bridge uses Core's actual job ID rather than
guessing a text ID. A lost capture response uses the same read-only discovery.
The
receipt echoes every host identity field, including `attachmentUnavailable`
when the host is replaying without bytes. An in-memory queue entry, subprocess
attempt, or handoff file is never proof of adoption.

The capture path admits up to eight concurrent operations and does not wait for
the old controller's OCR/AI queue. On pressure, startup failure, Core outage,
identity conflict, or uncertain status, it returns `handled=false` with no
adoption. A missing original returns the host's matching
`finance-ingress-refusal-v1` / `reupload_required` only when Core says the intake
does not exist or the matching job's original is missing. The host persists that
refusal as quarantine and asks for reupload. No Bridge outbound posting or
runtime activation is included in this contract.
