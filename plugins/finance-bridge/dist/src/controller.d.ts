import type { PluginHookInboundClaimContext, PluginHookInboundClaimEvent, PluginHookInboundClaimResult } from "openclaw-sdk/plugin-sdk/plugin-entry";
import { type BridgeRequest, type BridgeResponse } from "./protocol.js";
import type { HandoffPublisher } from "./handoff.js";
import type { FinanceAgentConfigRefusalV2 } from "./finance-agent-runtime-v2.js";
import type { FinanceAgentConfigProjectionV2 } from "./agent-profile-projection-v2.js";
import { type ReceiptMediaAdapter } from "./media.js";
export declare const FINANCE_FAILURE_REPLY = "Finance intake could not be processed safely. Please retry.";
export interface BridgeRunner {
    run(request: BridgeRequest, deadlineMs: number, inheritedFd?: number): Promise<BridgeResponse>;
}
interface ReceiptDependencies {
    media: ReceiptMediaAdapter;
    handoff: HandoffPublisher;
}
export interface FinanceLlmCompletion {
    text: string;
    provider: string | null;
    model: string | null;
    agentId: string | null;
    usage: {
        inputTokens?: number;
        outputTokens?: number;
    };
    audit: {
        caller: {
            kind: "plugin" | "context-engine" | "host" | "unknown";
            id?: string | null;
            name?: string | null;
        };
        purpose?: string | null;
        sessionKey?: string;
    };
}
export interface FinanceLlmRuntime {
    currentProjection(): FinanceAgentConfigProjectionV2 | FinanceAgentConfigRefusalV2;
    complete(params: {
        messages: Array<{
            role: "user";
            content: string;
        }>;
        model: string;
        maxTokens: number;
        temperature: 0;
        systemPrompt: string;
        purpose: string;
        agentId: string;
        maxRetries: 0;
        signal: AbortSignal;
    }): Promise<FinanceLlmCompletion>;
    recordStageTiming?(stage: "config_projection" | "receipt_lookup_and_bridge_prepare" | "host_completion" | "result_persistence", elapsedMs: number): void;
}
export declare class FinanceInboundController {
    private readonly workspaceRoot;
    private readonly runner;
    private readonly receipt?;
    private readonly controllerDeadlineMs;
    private readonly llmRuntime?;
    private queue;
    private queuedTurns;
    constructor(workspaceRoot: string, runner: BridgeRunner, receipt?: ReceiptDependencies | undefined, controllerDeadlineMs?: number, llmRuntime?: FinanceLlmRuntime | undefined);
    handle(event: PluginHookInboundClaimEvent, context: PluginHookInboundClaimContext): Promise<PluginHookInboundClaimResult>;
    private runWholeCardTurn;
    private runGuidedEditTurn;
    private runReceiptTurn;
    private runTextTurn;
    private proposeAndReviewCaptured;
    private proposeAndReview;
    private reviewProposal;
    private processingFooter;
    private runAiFallback;
}
export {};
