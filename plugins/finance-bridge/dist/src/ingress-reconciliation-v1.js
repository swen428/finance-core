import { createHash } from "node:crypto";
import { createBridgeRequest } from "./protocol.js";
import { caption, checkedStatus, validateTrustedIngressTurn, } from "./trusted-ingress.js";
const COMMAND_DEADLINE_MS = 30_000;
const JOB_ID = /^fcj_[0-9a-f]{40}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const NONCE = /^[\x21-\x7e]{16,128}$/u;
const CAPTURE_STATUSES = new Set([
    "captured", "processing", "awaiting_user", "needs_attention", "result_ready",
]);
const FINANCIAL_STATES = new Set([
    "unposted", "unknown", "rejected", "awaiting_confirmation", "needs_attention",
    "posting", "finalized", "corrected",
]);
const REPLY_STATUSES = new Set(["pending", "outcome_unknown", "sent"]);
const NEXT_ACTIONS = new Set([
    "none", "attention_required", "await_human_confirmation", "enqueue_existing_result",
    "guided_command_required", "d1_command_required", "prepare_child_review",
    "prepare_initial_review", "resume_accepted_posting", "trusted_result_verifier_required",
    "ai_outcome_unknown", "ai_prepared_attention", "ai_child_lineage_attention",
    "ai_non_child_result", "child_review_required", "capture_retry_deferred",
    "capture_in_progress", "capture_processing_required",
]);
export const FINANCE_INGRESS_RECONCILIATION_CAPABILITY = "telegram.finance-ingress-reconciliation-v1";
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function replyState(view) {
    if (!Array.isArray(view.reply_outbox) || view.reply_outbox.length > 100)
        return undefined;
    const states = [];
    for (const item of view.reply_outbox) {
        if (!isRecord(item) || typeof item.status !== "string" || !REPLY_STATUSES.has(item.status)) {
            return undefined;
        }
        states.push(item.status);
    }
    if (view.next_action === "enqueue_existing_result")
        return "missing";
    if (states.includes("outcome_unknown"))
        return "outcome_unknown";
    if (states.includes("pending"))
        return "pending";
    if (states.includes("sent"))
        return "sent";
    return "none";
}
export class FinanceIngressReconciliationV1 {
    workspaceRoot;
    runner;
    handoff;
    constructor(workspaceRoot, runner, handoff) {
        this.workspaceRoot = workspaceRoot;
        this.runner = runner;
        this.handoff = handoff;
    }
    async reconcile(request) {
        if (typeof request.nonce !== "string" || !NONCE.test(request.nonce)) {
            throw new Error("Finance reconciliation nonce is invalid.");
        }
        const refuse = (reason) => ({
            schema: "finance-ingress-reconciliation-v1", kind: "refused", nonce: request.nonce, reason,
        });
        let turn;
        try {
            turn = validateTrustedIngressTurn(request.event, request.context);
            if (turn?.photo)
                caption(turn.text);
        }
        catch {
            return refuse("invalid_original");
        }
        if (turn === undefined || turn.ingress.attachmentUnavailable === true)
            return refuse("invalid_original");
        try {
            // Pending handoff state needs the capture owner's recovery path; this
            // read-only entry must not clean it up or claim custody for the Host.
            if (await this.handoff.pendingPublicationKeyHash() !== undefined ||
                (await this.handoff.pendingReclaims()).length !== 0)
                return refuse("handoff_residue");
        }
        catch {
            return refuse("handoff_residue");
        }
        const identity = {
            workspace_path: this.workspaceRoot,
            operator_actor_id: String(turn.senderId),
            telegram_account_id: turn.ingress.accountId,
            telegram_conversation_id: turn.ingress.chatId,
            conversation_binding_id: turn.ingress.bindingId,
            telegram_message_id: turn.messageId,
        };
        let jobId;
        try {
            const discovery = await this.runner.run(createBridgeRequest("get_capture_job_for_message", identity), COMMAND_DEADLINE_MS);
            if (discovery.status !== "ok")
                return refuse("core_unavailable");
            if (discovery.result.candidate === null)
                return refuse("no_job");
            const candidate = discovery.result.candidate;
            if (!isRecord(candidate) || candidate.telegram_message_id !== turn.ingress.messageId ||
                typeof candidate.job_public_id !== "string" || !JOB_ID.test(candidate.job_public_id) ||
                typeof candidate.source_identity_sha256 !== "string" ||
                !SHA256.test(candidate.source_identity_sha256))
                return refuse("evidence_conflict");
            const sourceIdentity = JSON.stringify({
                authenticated_actor_id: String(turn.senderId),
                conversation_binding_id: turn.ingress.bindingId,
                source_message_id: turn.ingress.messageId,
                telegram_account_id: turn.ingress.accountId,
                telegram_conversation_id: turn.ingress.chatId,
                version: "finance_d2_telegram_source_context_v1",
            });
            if (candidate.source_identity_sha256 !==
                createHash("sha256").update(sourceIdentity, "utf8").digest("hex")) {
                return refuse("evidence_conflict");
            }
            jobId = candidate.job_public_id;
        }
        catch {
            return refuse("core_unavailable");
        }
        let adoption;
        let captureStatus;
        let ingressIdentityDigest;
        try {
            const status = await this.runner.run(createBridgeRequest("get_status", {
                workspace_path: this.workspaceRoot, job_public_id: jobId,
            }), COMMAND_DEADLINE_MS);
            if (status.status !== "ok")
                return refuse("incomplete_intake");
            // Core's intake status carries a legacy constant false for this field;
            // current financial state is checked only through authenticated recovery.
            const verified = checkedStatus(status.result, turn, jobId, false);
            if (verified === undefined)
                return refuse("incomplete_intake");
            const job = status.result.capture_job;
            if (!isRecord(job) || typeof job.status !== "string" ||
                !CAPTURE_STATUSES.has(job.status))
                return refuse("evidence_conflict");
            adoption = verified;
            captureStatus = job.status;
            ingressIdentityDigest = job.ingress_identity_digest;
        }
        catch {
            return refuse("core_unavailable");
        }
        if (!turn.photo) {
            try {
                const response = await this.runner.run(createBridgeRequest("get_interaction_route", identity), COMMAND_DEADLINE_MS);
                const route = response.status === "ok" ? response.result.interaction_route : undefined;
                const routeJob = response.status === "ok" ? response.result.capture_job : undefined;
                if (response.status !== "ok" || response.result.found !== true ||
                    !isRecord(route) || !isRecord(routeJob) ||
                    route.job_public_id !== adoption.jobId ||
                    route.raw_text_sha256 !== createHash("sha256").update(turn.text, "utf8").digest("hex") ||
                    route.authenticated_actor_id !== String(turn.senderId) ||
                    route.telegram_account_id !== turn.ingress.accountId ||
                    route.telegram_conversation_id !== turn.ingress.chatId ||
                    route.conversation_binding_id !== turn.ingress.bindingId ||
                    route.telegram_message_id !== turn.messageId ||
                    !["initial_intake", "whole_card", "guided_update", "guided_complete", "control_refused"].includes(String(route.route_kind)) ||
                    routeJob.public_id !== adoption.jobId || routeJob.intake_public_id !== adoption.intakeId ||
                    routeJob.capture_kind !== "text" || routeJob.attachment_content_hash !== null ||
                    routeJob.ingress_identity_digest !== ingressIdentityDigest) {
                    return refuse("evidence_conflict");
                }
            }
            catch {
                return refuse("core_unavailable");
            }
        }
        try {
            const recovery = await this.runner.run(createBridgeRequest("get_capture_recovery", {
                workspace_path: this.workspaceRoot,
                job_public_id: adoption.jobId,
                operator_actor_id: String(turn.senderId),
                telegram_account_id: turn.ingress.accountId,
                telegram_conversation_id: turn.ingress.chatId,
                conversation_binding_id: turn.ingress.bindingId,
            }), COMMAND_DEADLINE_MS);
            if (recovery.status !== "ok")
                return refuse("recovery_unavailable");
            const view = recovery.result;
            const reply = replyState(view);
            if (view.job_public_id !== adoption.jobId || view.capture_status !== captureStatus ||
                typeof view.financial_state !== "string" ||
                !FINANCIAL_STATES.has(view.financial_state) ||
                typeof view.next_action !== "string" || !NEXT_ACTIONS.has(view.next_action) ||
                view.next_action === "trusted_result_verifier_required" ||
                ((view.financial_state === "finalized" || view.financial_state === "corrected") &&
                    !isRecord(view.result)) || reply === undefined) {
                return refuse("evidence_conflict");
            }
            if (await this.handoff.pendingPublicationKeyHash() !== undefined ||
                (await this.handoff.pendingReclaims()).length !== 0)
                return refuse("handoff_residue");
            return {
                schema: "finance-ingress-reconciliation-v1", kind: "matched", nonce: request.nonce,
                adoption, captureStatus, financialState: view.financial_state, replyState: reply,
            };
        }
        catch {
            return refuse("recovery_unavailable");
        }
    }
}
