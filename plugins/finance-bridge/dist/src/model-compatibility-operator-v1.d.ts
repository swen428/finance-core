import { type FinanceAgentConfigProjectionV2 } from "./agent-profile-projection-v2.js";
import { type ArtifactHashResultV1 } from "./artifact-hash-v1.js";
import { type FinanceBridgeConfig } from "./config.js";
import type { BridgeRunner } from "./controller.js";
import { type CoreDistributionEvidenceV1 } from "./core-distribution-v1.js";
import { type JsonObject } from "./protocol.js";
export declare const COMPATIBILITY_CASE_DEADLINE_MS = 30000;
export declare const OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION = "finance-compatibility-failure-envelope-v1";
export declare const OPERATOR_FAILURE_CATEGORIES: readonly ["AI_MODEL_EVAL_REFUSED", "AUTH_UNAVAILABLE", "ARTIFACT_REFUSED", "BRIDGE_REFUSED", "COMPATIBILITY_REFUSED", "CONFIG_REFUSED", "REHEARSAL_TIMEOUT", "OPERATOR_REFUSED"];
export type OperatorFailureCategoryV1 = typeof OPERATOR_FAILURE_CATEGORIES[number];
export declare const OPERATOR_VERIFICATION_REASONS: readonly ["ACCEPTANCE_POLICY_INVALID", "AMBIGUITY_FLAGS_EXTRA", "AMBIGUITY_FLAGS_MISMATCH", "AMBIGUITY_FLAGS_MISSING", "CASE_IDENTITY_INVALID", "CONFIDENCE_SHAPE_INVALID", "CONFIDENCE_VALUE_INVALID", "EVIDENCE_REFERENCE_INVALID", "EVIDENCE_REFS_SHAPE_INVALID", "FIELD_MISMATCH", "HARNESS_CONTRACT_INVALID", "HARNESS_OUTCOME_FIELDS_INVALID", "NULL_FIELD_HAS_EVIDENCE", "PRESENT_FIELD_LACKS_EVIDENCE", "PROMPT_INJECTION_ECHO", "RESPONSE_ADMISSION_INVALID", "RESPONSE_DUPLICATE_KEY", "RESPONSE_ENCODING_INVALID", "RESPONSE_JSON_INVALID", "RESPONSE_NOT_OBJECT", "RESPONSE_OVERSIZED", "RESPONSE_SCHEMA_INVALID", "RESPONSE_UTF8_INVALID", "VERIFIER_INPUT_INVALID"];
export type OperatorVerificationReasonV1 = typeof OPERATOR_VERIFICATION_REASONS[number];
export declare const OPERATOR_FAILURE_PHASES: readonly ["preflight", "artifact", "config", "health", "compatibility_case", "python_verification", "receipt_registration", "rehearsal_register", "unknown"];
export type OperatorFailurePhaseV1 = typeof OPERATOR_FAILURE_PHASES[number];
export declare const OPERATOR_TIMER_LAYERS: readonly ["none", "compatibility_case", "rehearsal_register"];
export type OperatorTimerLayerV1 = typeof OPERATOR_TIMER_LAYERS[number];
export interface OperatorFailureEnvelopeV1 {
    schema_version: typeof OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION;
    category: OperatorFailureCategoryV1;
    phase: OperatorFailurePhaseV1;
    timer_layer: OperatorTimerLayerV1;
    elapsed_ms: number;
    timeout_triggered: boolean;
    verification_reason?: OperatorVerificationReasonV1;
    verification_field?: string;
}
export declare class OperatorFailureV1 extends Error {
    readonly envelope: OperatorFailureEnvelopeV1;
    constructor(message: string, envelope: OperatorFailureEnvelopeV1);
}
export declare function operatorFailureEnvelopeV1(error: unknown): OperatorFailureEnvelopeV1;
export declare function createOperatorFailureV1(message: string, details: Omit<OperatorFailureEnvelopeV1, "schema_version">): OperatorFailureV1;
export interface CompatibilityCaseV1 {
    case_id: string;
    source_kind: "telegram_text" | "receipt_local_ocr_text";
    catalog: Record<string, string>;
    parent_payload: Record<string, unknown>;
    ocr_layout: Record<string, unknown> | null;
    expected: Record<string, unknown>;
    forbidden_output_fragments: string[];
}
export interface CompatibilityFixtureSetV1 {
    version: string;
    cases: CompatibilityCaseV1[];
}
export interface CompatibilityAssetsV1 {
    prompt: string;
    fixtures: CompatibilityFixtureSetV1;
    identity_sha256: string;
}
export interface CompatibilityLlmResultV1 {
    text: string;
    provider: string;
    model: string;
    agentId: string;
    usage: Record<string, unknown>;
    audit: {
        caller: {
            kind: string;
            id?: string;
            name?: string;
        };
        purpose?: string;
        sessionKey?: string;
    };
}
export interface CompatibilityLlmRuntimeV1 {
    capabilities?: {
        maxRetries?: unknown;
    };
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
        agentId: "finance";
        maxRetries: 0;
        signal: AbortSignal;
    }): Promise<CompatibilityLlmResultV1>;
}
export interface CompatibilityHarnessOutcomeV1 {
    case_id: string;
    ordinary_agent_turn_count: 0;
    isolated_completion_count: 1;
    provider_dispatch_count: 1;
    effective_max_retries: 0;
    elapsed_ms: number;
    observed_provider: string;
    observed_model: string;
    observed_agent_id: "finance";
    response_utf8_b64: string;
}
export interface CompatibilityArtifactEvidenceV1 {
    openclaw: ArtifactHashResultV1;
    plugin: ArtifactHashResultV1;
    core: CoreDistributionEvidenceV1;
    finance_commit: string;
    runtime_commit: string;
}
export declare function loadCompatibilityAssetsV1(coreDistributionRootValue: string): Promise<CompatibilityAssetsV1>;
export declare function runCompatibilityHarnessV1(params: {
    runtime: CompatibilityLlmRuntimeV1;
    runner: BridgeRunner;
    projection: FinanceAgentConfigProjectionV2;
    assets: CompatibilityAssetsV1;
    now?: () => number;
    deadlineMs?: number;
}): Promise<CompatibilityHarnessOutcomeV1[]>;
export declare function verifyArtifactEvidenceV1(params: {
    config: FinanceBridgeConfig;
    openclawArtifactRoot: string;
    loadedPluginRoot: string;
    loadedPluginSource: string;
    expectedOpenclawVersion: string;
}): Promise<CompatibilityArtifactEvidenceV1>;
export declare function registerCompatibilityReceiptV1(params: {
    runner: BridgeRunner;
    workspaceRoot: string;
    projection: FinanceAgentConfigProjectionV2;
    outcomes: CompatibilityHarnessOutcomeV1[];
}): Promise<JsonObject>;
