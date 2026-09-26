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

The pinned Host's Finance hook must set photo `event.content` to the original
Telegram caption (`''` when absent). The Bridge omits Core `caption` for an
empty string, refuses nonempty whitespace-only captions before any Core
discovery or adoption, and preserves valid captions byte for byte, including
leading or trailing spaces and a literal `<media:image>`.
The public repository tests the consumer behavior; the fixed Host's producer
projection and same-version interoperability remain separate host acceptance.

For a trusted photo, the Host supplies one local original with matching
`mediaPath`, `mediaUrl`, one-element `mediaPaths` and `mediaUrls`, and matching
`mediaType` and one-element `mediaTypes`. Only after trusted ingress validation
does the Bridge read a direct child of `getMediaDir()/inbound` (allowing the
macOS `/var` to `/private/var` spelling). It pins the private 0700 directory
and opens the leaf without following a symlink. The original must be an
owner-owned regular file with mode 0600 or 0644; its size, inode, timestamps,
permissions, and content are checked across the read. The older controller's
opaque `media://inbound/` reader and the handoff 0600 reader retain their
existing stricter contracts. No Host original is chmodded or removed.

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
The Bridge compares Core's saved `raw-intake-v1` fingerprint with the exact
Telegram image intake material: original caption (or Core's absent-caption
placeholder), message source, and attachment hash. A prior photo job with the
same ingress identity and original bytes but a different caption cannot be
adopted, including on replay or after a lost capture response.
For text, including control text, the Bridge additionally queries Core's
authenticated `get_interaction_route` for the same actor, account, conversation,
binding, and message. Adoption requires the frozen route to match the exact raw
text hash and the same intake, job, kind, and ingress digest. An old text job
without a frozen route, an incomplete route, or a failed lookup cannot be adopted,
including after a lost capture response or on replay.
For newly captured photos, adoption also requires a durable per-slot reclaim
intent and completed handoff cleanup. The intent is only a request to check
Core; it is never proof that Core holds an original. Core `get_status` must match
the saved intake, job, ingress digest, and original hash immediately before any
cleanup. This read can recover a lost capture response after a committed write. The
Bridge first calls `get_capture_job_for_message` within the authenticated
binding. An already captured message is read and verified without downloading
or writing the image again. Text intake IDs may be UUIDs, while receipt IDs are
derived from the capture key; the Bridge uses Core's actual job ID rather than
guessing a text ID. A lost capture response uses the same read-only discovery.
The receipt echoes every host identity field, including `attachmentUnavailable`
when the host is replaying without bytes. An in-memory queue entry, subprocess
attempt, or handoff file is never proof of adoption.

The Bridge keeps the handoff flock while it pins the record, image, and
directory identities and durably writes the reclaim intent. It releases the
flock before querying Core. With verified Core custody, it reacquires the
flock, checks the saved bytes and inode identities, then removes the image,
record, and intent in that order using directory-relative identity-checked
unlink and a directory fsync after each removal. On startup, it enumerates
intents and rechecks Core before resuming only the recognized crash states:
image+record+intent, record+intent, or intent alone. A failed Core capture
leaves the image and intent in place; the same host ingress can retry capture.
Unknown residue, altered image/record evidence, or unavailable Core proof
blocks cleanup and adoption. A crash after Core commit but before intent
creation depends on host spool replay to reseal that slot. Older slots with
no trustworthy intent are retained and count against the 32-slot quota;
capacity never authorizes deletion.
An empty, partial, or corrupt intent also blocks adoption and leaves the
original record and image intact for controlled operator repair. It is not
deleted automatically by age or capacity. Recovery gives Core queries and
lock acquisition the remaining time in a 45-second budget and checks elapsed
time after each proof. A filesystem call may take longer than that budget;
expiry leaves the Bridge unready or returns `handled=false` without a custody
receipt.

The capture path admits up to eight concurrent operations and does not wait for
the old controller's OCR/AI queue. On pressure, startup failure, Core outage,
identity conflict, or uncertain status, it returns `handled=false` with no
adoption. A missing original returns the host's matching
`finance-ingress-refusal-v1` / `reupload_required` only when Core says the intake
does not exist or the matching job's original is missing. The host persists that
refusal as quarantine and asks for reupload. No Bridge outbound posting or
runtime activation is included in this contract.

Validation uses synthetic temporary Core workspaces and fake host events,
including 40 distinct JPEG/PNG updates, restart and response loss, each
reclaim unlink/fsync stage, a tampered Core original, and a separate-process
handoff lock. The CI Bridge matrix runs it on Ubuntu and macOS. The tested
recovery scope is process crash/restart with the documented fsync calls and
cooperating processes. It does not claim arbitrary power-loss durability or
atomic protection against a malicious same-UID process mutating files outside
the flock. `payloadSha256` remains host-provided trust evidence, not a digest
independently reconstructed by this Bridge slice.
