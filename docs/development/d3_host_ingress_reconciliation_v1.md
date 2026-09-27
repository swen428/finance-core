# D3-4 Host ingress reconciliation v1

The Host may ask Finance Bridge to reconcile one isolated, retained Telegram
original after its capture acknowledgement is lost. The Host supplies the exact
saved `inbound_claim` event and context plus a fresh ASCII nonce. Full Bridge
registration requires the exact singleton Host capability
`telegram.finance-ingress-reconciliation-v1` and the separate Host method
`registerFinanceIngressReconciliationV1(handler)`. The method is not part of the
published SDK in this slice; a Host without it fails registration. This source
contract alone does not activate a runtime.

The handler returns `finance-ingress-reconciliation-v1`, echoes the nonce, and
either refuses with a finite reason or returns `kind: matched` with the existing
`finance-ingress-adoption-v1` locator, `captureStatus`, `financialState`, and
`replyState`. A match means only that the original message and its current
Core evidence were verified. It does not authorize a new capture, processing,
AI invocation, finalization, reply send, or retry. The Host must independently
verify the saved original and exact adoption fields before completing its own
isolated acknowledgement under its lease and compare-and-swap rules.

Bridge reads, in order, `get_capture_job_for_message`, `get_status`,
`get_interaction_route` for text, and `get_capture_recovery`. It checks the
authenticated owner, account, conversation, binding, message and original
ingress digest. Text requires its original route and exact raw text hash; photos
require the original attachment hash, exact caption fingerprint, and verified
stored original. Missing or partial jobs, changed originals, absent text routes,
unavailable images, Core conflicts, and unresolved handoff residue refuse.
`financialState` comes from authenticated `get_capture_recovery`; the
`get_status.final_transaction_created` field is not a financial result check.
The result contains no raw text, attachment bytes, financial amount, or reply
payload. It is a bounded locator and status view, not a transport receipt.
