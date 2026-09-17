import type { OpenClawPluginApi } from "openclaw-sdk/plugin-sdk/plugin-entry";
import { type FinanceBridgeConfig } from "./config.js";
import type { BridgeRunner } from "./controller.js";
import { runCompatibilityHarnessV1, verifyArtifactEvidenceV1 } from "./model-compatibility-operator-v1.js";
import { verifyPlatformArtifactReceiptV1 } from "./platform-artifact-verifier-v1.js";
export interface OperatorCliDependenciesV1 {
    validateConfig(value: unknown): Promise<FinanceBridgeConfig>;
    createRunner(config: FinanceBridgeConfig): BridgeRunner;
    resolveOpenClawRoot(): Promise<string>;
    writeOutput(value: unknown): void;
    verifyArtifactEvidence?: typeof verifyArtifactEvidenceV1;
    verifyPlatformArtifact?: typeof verifyPlatformArtifactReceiptV1;
    runCompatibilityHarness?: typeof runCompatibilityHarnessV1;
}
export declare function executeCompatibilityRegistrationV1(params: {
    api: OpenClawPluginApi;
    dependencies?: OperatorCliDependenciesV1;
}): Promise<Record<string, unknown>>;
export declare function registerOperatorCliV1(api: OpenClawPluginApi, dependencies?: OperatorCliDependenciesV1): void;
