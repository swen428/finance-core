import type { PluginHookInboundClaimContext, PluginHookInboundClaimEvent } from "openclaw-sdk/plugin-sdk/plugin-entry";
import type { BridgeRunner } from "./controller.js";
import type { HandoffPublisher } from "./handoff.js";
import { type FinanceIngressAdoption } from "./trusted-ingress.js";
export declare const FINANCE_INGRESS_RECONCILIATION_CAPABILITY: "telegram.finance-ingress-reconciliation-v1";
/** The Host passes its isolated, retained original, never a fresh Telegram delivery. */
export interface FinanceIngressReconciliationRequestV1 {
    event: PluginHookInboundClaimEvent;
    context: PluginHookInboundClaimContext;
    nonce: string;
}
export type FinanceIngressReconciliationRefusalReasonV1 = "invalid_original" | "no_job" | "evidence_conflict" | "incomplete_intake" | "handoff_residue" | "core_unavailable" | "recovery_unavailable";
export type FinanceIngressReconciliationResultV1 = {
    schema: "finance-ingress-reconciliation-v1";
    kind: "matched";
    nonce: string;
    adoption: FinanceIngressAdoption;
    captureStatus: string;
    financialState: string;
    replyState: "none" | "missing" | "pending" | "outcome_unknown" | "sent";
} | {
    schema: "finance-ingress-reconciliation-v1";
    kind: "refused";
    nonce: string;
    reason: FinanceIngressReconciliationRefusalReasonV1;
};
export declare class FinanceIngressReconciliationV1 {
    private readonly workspaceRoot;
    private readonly runner;
    private readonly handoff;
    constructor(workspaceRoot: string, runner: BridgeRunner, handoff: Pick<HandoffPublisher, "pendingPublicationKeyHash" | "pendingReclaims">);
    reconcile(request: FinanceIngressReconciliationRequestV1): Promise<FinanceIngressReconciliationResultV1>;
}
