import type { PluginHookInboundClaimContext, PluginHookInboundClaimEvent, PluginHookInboundClaimResult } from "openclaw-sdk/plugin-sdk/plugin-entry";
import type { BridgeRunner } from "./controller.js";
import { type HandoffPublisher } from "./handoff.js";
import type { ReceiptMediaAdapter } from "./media.js";
import { type JsonObject } from "./protocol.js";
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
export interface ValidatedTurn {
    ingress: TrustedFinanceIngress;
    chatId: number;
    messageId: number;
    senderId: number;
    date: number;
    text: string;
    photo: boolean;
}
export declare function hasTrustedFinanceIngress(event: PluginHookInboundClaimEvent): boolean;
export declare function validateTrustedIngressTurn(event: PluginHookInboundClaimEvent, context: PluginHookInboundClaimContext): ValidatedTurn | undefined;
export declare function caption(text: string): string | undefined;
/** Mirror Core's exact-caption Telegram photo fingerprint. */
export declare function photoIntakeFingerprint(chatId: number, messageId: number, rawCaption: string, attachmentHash: string): string;
export declare function checkedStatus(result: JsonObject, turn: ValidatedTurn, expectedJobId: string, requireLegacyFinalTransactionField?: boolean): FinanceIngressAdoption | undefined;
export declare class TrustedIngressCapture {
    private readonly workspaceRoot;
    private readonly runner;
    private readonly media;
    private readonly handoff;
    private inFlight;
    private readonly activeByMessage;
    constructor(workspaceRoot: string, runner: BridgeRunner, media: ReceiptMediaAdapter, handoff: HandoffPublisher);
    private verifyCoreCustody;
    /** Called at plugin readiness, including when the Host has already ACKed. */
    resumePendingReclaims(maxDurationMs?: number): Promise<void>;
    handle(event: PluginHookInboundClaimEvent, context: PluginHookInboundClaimContext): Promise<TrustedClaimResult>;
}
