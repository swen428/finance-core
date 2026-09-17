import type { JsonObject } from "./protocol.js";
export interface ProcessingStatusV2 {
    intakePublicId: string;
    attemptPublicId: string | null;
    receiptPublicId: string | null;
    admissionDecisionPublicId: string | null;
    processingPath: string;
    safeReasonCode: string | null;
    displayAlias: string | null;
    canonicalAttribution: {
        provider: string;
        model: string;
        agentId: "finance";
    } | null;
    attributionMatch: boolean | null;
}
export declare function parseProcessingStatusV2(value: JsonObject): ProcessingStatusV2;
export declare function renderProcessingFooterV2(status: ProcessingStatusV2): string;
export declare function renderProcessingStatusDetailV2(status: ProcessingStatusV2): string;
