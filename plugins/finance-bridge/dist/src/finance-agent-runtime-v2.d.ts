import { type FinanceAgentConfigProjectionV2 } from "./agent-profile-projection-v2.js";
import type { FinanceAgentProfileEvidenceV2 } from "./config.js";
export interface FinanceAgentConfigRefusalV2 {
    schema_version: "finance-openclaw-agent-config-refusal-v2";
    refusal_code: "projection_invalid";
    evidence_sha256: string;
}
export interface LoadedPluginSourceEvidenceV2 {
    pluginId: string;
    packageName: string;
    source: string;
    rootDir: string;
    origin: string;
    status: string;
    providerIds: string[];
    sourceConfigEntrySha256: string;
}
export declare function getLoadedCodexPluginSourceV2(runtimeValue: unknown): unknown;
export declare function getLoadedOpenAIPluginSourceV2(runtimeValue: unknown): unknown;
export interface ReviewedFinanceHostRuntimeV2 {
    hostConfig: Record<string, unknown>;
    codexPluginSource: Record<string, unknown>;
    openaiPluginSource: Record<string, unknown>;
}
export declare function resolveReviewedFinanceHostRuntimeV2(runtimeValue: unknown): ReviewedFinanceHostRuntimeV2;
export declare function deriveFinanceAgentProjectionV2(hostConfig: unknown, evidence: FinanceAgentProfileEvidenceV2, openclawVersion: string, loadedCodexPluginSource: unknown, loadedOpenAiPluginSource: unknown): FinanceAgentConfigProjectionV2;
export declare function financeProjectionOrRefusalV2(hostConfig: unknown, evidence: FinanceAgentProfileEvidenceV2, openclawVersion: string, loadedCodexPluginSource: unknown, loadedOpenAiPluginSource: unknown): FinanceAgentConfigProjectionV2 | FinanceAgentConfigRefusalV2;
export declare function validateFinanceHostPolicyV2(hostConfig: unknown): void;
export declare function validateLoadedFinancePluginRootV2(hostConfig: unknown, loadedPluginRoot: string): void;
export declare function validateReviewedPluginSourcesV2(hostConfig: unknown, loadedFinancePluginRoot: string, openclawArtifactRoot: string, loadedCodexPluginSource: unknown, loadedOpenAiPluginSource: unknown): void;
