import type { PluginHookInboundClaimContext, PluginHookInboundClaimEvent, PluginHookInboundClaimResult } from "openclaw-sdk/plugin-sdk/plugin-entry";
import type { BridgeRunner } from "./controller.js";
import type { HandoffPublisher } from "./handoff.js";
import type { ReceiptMediaAdapter } from "./media.js";
/** These fields are copied from the pinned host contract; SDK packages may lag the pinned host. */
export interface TrustedFinanceIngress {
    channel: "telegram";
    accountId: string;
    updateId: number;
    chatId: string;
    messageId: string;
    senderId: string;
    bindingId: string;
    payloadSha256: string;
    attachmentSha256?: string;
    attachmentUnavailable?: true;
}
export interface FinanceIngressAdoption extends TrustedFinanceIngress {
    schema: "finance-ingress-adoption-v1";
    intakeId: string;
    jobId: string;
    attachmentStatus: "none" | "stored";
}
export interface FinanceIngressRefusal extends TrustedFinanceIngress {
    schema: "finance-ingress-refusal-v1";
    kind: "reupload_required";
}
export type TrustedClaimResult = PluginHookInboundClaimResult & {
    adoption?: FinanceIngressAdoption;
    financeIngressRefusal?: FinanceIngressRefusal;
};
export declare function hasTrustedFinanceIngress(event: PluginHookInboundClaimEvent): boolean;
export declare class TrustedIngressCapture {
    private readonly workspaceRoot;
    private readonly runner;
    private readonly media;
    private readonly handoff;
    private inFlight;
    private readonly activeByMessage;
    constructor(workspaceRoot: string, runner: BridgeRunner, media: ReceiptMediaAdapter, handoff: HandoffPublisher);
    handle(event: PluginHookInboundClaimEvent, context: PluginHookInboundClaimContext): Promise<TrustedClaimResult>;
}
