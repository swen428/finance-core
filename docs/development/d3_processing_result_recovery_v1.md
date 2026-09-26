# D3-3 processing and committed-result recovery v1

This slice resumes one **known** durable capture job by its public ID. The two
Bridge commands are `get_capture_recovery` (read only) and
`resume_capture_recovery`. GET returns the current recovery view and a
`recovery_step_token` derived from that view. RESUME requires that token, and
its idempotency key binds both the job ID and token. A matching token permits
at most one local action: process the capture, replay one frozen D1/guided
command, prepare an initial or eligible child D2 review, resume an accepted D2
posting attempt, or enqueue one already committed result reply. The Bridge
then returns a fresh view and token for the next step.

If the token is stale at command selection, RESUME returns the current view with
`stale_recovery_step=true` and `performed_action=none`; it does not advance the
newly available step. Replaying the old request therefore cannot repeat the
next action. If another worker advances a stage after selection, the underlying
processor or business command checks its own persisted authority and replay
key; a processor replay reports `performed_action=none`. Selection and
dispatch are not one atomic transaction. Neither command scans for jobs. Both require the complete
authenticated Telegram actor, account, conversation and binding context.
Their response keeps the original interaction's outcome separate from the
source economic event's current state. An edit refusal never becomes a
successful expense merely because the source expense was posted.

The saved capture job and immutable interaction route identify the original
message. A whole-card or guided route is resolved through its saved card,
session, operation and source lineage. A control replay re-enters the existing
D1/guided handler with that frozen operation; it does not classify the text as
a new expense or create a new route. The control job remains bound to its
original message; its proposal pointer is not rebound to a D1 or AI child. An
AI child also leaves the source capture job's original proposal pointer
unchanged. Missing or ambiguous source evidence fails closed for local
attention. Recovery does not create a historical capture or route. Each
recovery query checks the authenticated source binding; writes are delegated
to the existing processing, review, posting, result/outbox or control
handlers, which recheck their own persisted source, operation, lease or attempt
authority.

Recovery first reads any committed control operation, D2 decision and attempt,
final result, and reply outbox. It then examines the current, verified proposal
lineage. `ensure_capture_review` is reserved for the original capture
proposal. A separate child-review action may prepare a D2 review for an active,
complete, unexpired D1 card, or for an AI child whose `proposal_created` result,
proposal link, parent and original source message all verify. The capture job
keeps its original proposal identity; an unlinked AI result or a classification
result cannot become a review. An accepted D2 attempt resumes with its original
attempt ID. Recovery never creates a confirmation or invokes a model. A
claimed AI invocation without a result remains `outcome_unknown`, even after a
restart or lease expiry. Local OCR keeps the D3-2 lease and retry budget.
Terminal receipt failures show `attention_required`; a retry waiting for its
saved deadline shows `capture_retry_deferred`; an active lease shows
`capture_in_progress`. RESUME does not claim that these states ran OCR.

After a posting or D2b correction commits, recovery verifies the existing
transaction and current effective correction head before enqueueing any
missing result reply outbox row. It enqueues one missing result per call. An
old correction keeps its historical ID while the displayed `current` values
reflect the newest verified version. The reply outbox is local state only:
this slice does not start or retry a Telegram send. If correction history
exists, the verifier and the queried job must use the same policy-bound
staging database connection. The policy's canonical database path must equal
the selected workspace database, and its owner actor must match the
authenticated actor. Missing or mismatched policy requires attention;
recovery cannot fall back to an unverified amount.

## Acceptance matrix

The references below identify checked-in test cases from source inspection;
this documentation pass did not execute them. “Partial” means the service or
adjacent lower-level contract has coverage, but the D3-3 command path or a
material failure boundary is not exercised end to end.

| Boundary | Required observable result | Checked-in evidence | Coverage |
| --- | --- | --- | --- |
| Recovery step token | GET returns a token; RESUME requires it and an idempotency key bound to job plus token. A replayed old token returns current state without advancing. | `tests/test_d3_capture_recovery_bridge_v1.py::test_known_initial_capture_can_resume_local_processing_without_new_fact`, `::test_resume_key_must_bind_one_job`, `::test_resume_requires_a_well_formed_step_token`, `::test_corrected_result_reopens_exact_policy_database_and_recovers_one_fact`, `::test_frozen_whole_card_recovery_applies_once_and_returns_original_card`; `tests/test_d3_capture_recovery_v1.py::test_committed_posting_restarts_and_enqueues_once` | Covered for missing/malformed tokens, key refusal, refreshed tokens, stale replay, and service reread mismatch. Concurrent state change during command selection is not separately injected. |
| Caller binding and known job | Recovery reads only the requested job under its saved actor/account/conversation/binding; an invalid binding fails closed. | `tests/test_d3_capture_recovery_v1.py::test_query_authenticates_full_identity_and_frozen_route`; `tests/test_d3_capture_recovery_bridge_v1.py::test_recovery_rejects_wrong_authenticated_binding` | Partial: wrong-binding GET and RESUME are covered; missing/ambiguous jobs and changed frozen-source cases are not. |
| One local processing action | A known captured text or JPEG receipt job advances through the existing processor and creates no final transaction. Terminal/deferred receipt states do not claim that processing ran. | `tests/test_d3_capture_recovery_bridge_v1.py::test_known_initial_capture_can_resume_local_processing_without_new_fact`, `::test_receipt_resume_uses_local_ocr_and_preserves_source`, `::test_receipt_failure_projection_does_not_claim_processing`, `::test_processing_advanced_after_token_check_reports_no_second_action`; processor lease/OCR cases in `tests/test_d3_capture_processing_v1.py` | Partial: lost response between sequential local stages is not separately injected. |
| Persisted AI outcome unknown | Recovery takes no action and creates no review; it never invokes the provider again. | `tests/test_d3_capture_recovery_v1.py::test_unknown_ai_never_reinvokes_or_prepares_card`; persisted-claim behavior in `tests/test_d3_capture_processing_v1.py::test_existing_ai_claim_without_result_is_unknown_and_never_reclaimed` | Partial: recovery sets the job status directly in its test; no reopened, claim-backed recovery case counts provider calls. |
| Frozen guided operation replay | After the business edit commits but settlement fails, replay the same operation and settle it once. A completed session whose review batch was not claimed resumes the original completion command. | `tests/test_d3_capture_recovery_bridge_v1.py::test_guided_business_commit_before_settlement_replays_same_operation`, `::test_guided_completion_commit_before_batch_claim_recovers_same_batch`; service-level lost-ack view in `tests/test_d3_capture_recovery_v1.py::test_guided_committed_edit_with_lost_ack_still_requests_replay` | Covered for guided update and completion's commit-before-batch boundary. |
| Frozen whole-card replay and D1 child review | Replay the frozen whole-card operation once, preserve the original card identity, then prepare one child review with the new step token. | `tests/test_d3_capture_recovery_bridge_v1.py::test_frozen_whole_card_recovery_applies_once_and_returns_original_card` | Covered for Bridge replay, stale-token replay and D1 child review; crash between card commit and review preparation is not injected here. |
| AI child review | Prepare a review only for a sealed, linked `proposal_created` child; never review a classification-only or unlinked result, and keep the capture job's original proposal ID. | `tests/test_d3_capture_recovery_v1.py::test_saved_ai_child_prepares_review_without_rebinding_capture_job`, `::test_non_child_ai_result_never_becomes_a_review` | Partial: service-level boundary is covered; no Bridge RESUME test prepares an AI child review with a recovery step token. |
| Accepted D2 attempt | Resume the same accepted attempt without a second confirmation; commit at most one transaction. | `tests/test_d3_capture_recovery_v1.py::test_accepted_d2_attempt_resumes_without_new_confirmation` | Partial: the recovery service is exercised after a crash immediately after acceptance commit; Bridge command dispatch and token replay are not. |
| Corrected posting, policy binding and missing reply rows | Reopen only the policy-bound database for the authenticated owner; recover the current corrected amount and enqueue the posting/correction replies once without adding a transaction. | `tests/test_d3_capture_recovery_bridge_v1.py::test_corrected_result_reopens_exact_policy_database_and_recovers_one_fact`; lower-level multi-correction coverage in `tests/test_d3_capture_result_outbox_v1.py::test_older_correction_reply_replays_current_verified_head` | Covered for correct, missing and mismatched policy, wrong actor, one correction, outbox recovery and stale-token replay. Bridge recovery with multiple correction versions is not covered. |
| Historical lineage and changed context | Admit only the documented legacy lineage; do not create a new route or recover against changed source identity. | Migration/route contracts in `tests/test_d3_legacy_lineage_cutover_v1.py` and `tests/test_d3_route_entry_authority_v1.py` | Partial: no recovery case starts from an admitted historical no-route job or a changed/ambiguous control source. |

These tests do not establish a full failure-injection matrix across capture,
OCR, proposal, review, every D1/AI child lineage, D2b
multiple-correction replay and outbox commits. The
processing-stage failure tests are documented separately in
[`d3_capture_processing_v1.md`](d3_capture_processing_v1.md); D3-3-specific
gaps remain visible above. D3-4 owns durable pending-job enumeration, worker
scheduling, ordered transport sends and recording transport acknowledgements.
D3-3 only prepares and recovers local outbox state; it does not send or
acknowledge Telegram messages and does not activate a finance runtime.
