import { canonicalProjectionSha256V2 } from "./agent-profile-projection-v2.js";
import { computeArtifactHashV1, computeObserverHashV1, resolveOpenClawArtifactRootV1, } from "./artifact-hash-v1.js";
import { validatePluginConfig } from "./config.js";
import { deriveFinanceAgentProjectionV2, resolveReviewedFinanceHostRuntimeV2, validateFinanceHostPolicyV2, validateReviewedPluginSourcesV2, } from "./finance-agent-runtime-v2.js";
import { loadCompatibilityAssetsV1, registerCompatibilityReceiptV1, runCompatibilityHarnessV1, operatorFailureEnvelopeV1, createOperatorFailureV1, OperatorFailureV1, verifyArtifactEvidenceV1, } from "./model-compatibility-operator-v1.js";
import { executeReceiptBoundCompatibilityV1, verifyPlatformArtifactReceiptV1, } from "./platform-artifact-verifier-v1.js";
import { createBridgeRequest } from "./protocol.js";
import { BridgeCliRunner, bridgeProcessFailureCategory } from "./subprocess.js";
function asOperatorFailure(error, details) {
    if (error instanceof OperatorFailureV1)
        return error;
    const message = error instanceof Error ? error.message : "Finance compatibility operator refused.";
    return createOperatorFailureV1(message, {
        ...details,
        elapsed_ms: details.elapsed_ms ?? 0,
        timeout_triggered: details.timeout_triggered ?? false,
    });
}
async function runOperatorPhase(phase, category, operation) {
    try {
        return await operation();
    }
    catch (error) {
        throw asOperatorFailure(error, { category, phase, timer_layer: "none" });
    }
}
const defaultDependencies = {
    validateConfig: validatePluginConfig,
    createRunner: (config) => new BridgeCliRunner(config),
    resolveOpenClawRoot: async () => await resolveOpenClawArtifactRootV1(process.argv[1] ?? ""),
    writeOutput: (value) => {
        process.stdout.write(`${JSON.stringify(value)}\n`);
    },
};
function operatorRuntime(api) {
    return api.runtime.llm;
}
export async function executeCompatibilityRegistrationV1(params) {
    const dependencies = params.dependencies ?? defaultDependencies;
    const verifyArtifacts = dependencies.verifyArtifactEvidence ?? verifyArtifactEvidenceV1;
    const verifyPlatform = dependencies.verifyPlatformArtifact ?? verifyPlatformArtifactReceiptV1;
    const runHarness = dependencies.runCompatibilityHarness ?? runCompatibilityHarnessV1;
    let config;
    try {
        config = await dependencies.validateConfig(params.api.pluginConfig);
    }
    catch {
        throw createOperatorFailureV1("Finance bridge configuration refused: CONFIG_REFUSED", {
            category: "CONFIG_REFUSED",
            phase: "config",
            timer_layer: "none",
            elapsed_ms: 0,
            timeout_triggered: false,
        });
    }
    const initialHostRuntime = await runOperatorPhase("config", "CONFIG_REFUSED", async () => resolveReviewedFinanceHostRuntimeV2(params.api.runtime));
    const initialHostConfig = initialHostRuntime.hostConfig;
    const initialCodexPluginSource = initialHostRuntime.codexPluginSource;
    const initialOpenAiPluginSource = initialHostRuntime.openaiPluginSource;
    await runOperatorPhase("config", "CONFIG_REFUSED", () => {
        validateFinanceHostPolicyV2(initialHostConfig);
    });
    if (params.api.rootDir === undefined) {
        throw createOperatorFailureV1("Loaded plugin root is unavailable.", {
            category: "ARTIFACT_REFUSED",
            phase: "artifact",
            timer_layer: "none",
            elapsed_ms: 0,
            timeout_triggered: false,
        });
    }
    const openclawArtifactRoot = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => await dependencies.resolveOpenClawRoot());
    await runOperatorPhase("artifact", "ARTIFACT_REFUSED", () => {
        validateReviewedPluginSourcesV2(initialHostConfig, params.api.rootDir, openclawArtifactRoot, initialCodexPluginSource, initialOpenAiPluginSource);
    });
    const evidenceParams = {
        config,
        openclawArtifactRoot,
        loadedPluginRoot: params.api.rootDir,
        loadedPluginSource: params.api.source,
        expectedOpenclawVersion: params.api.runtime.version,
    };
    const artifacts = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => (await verifyArtifacts({ ...evidenceParams })));
    const projection = await runOperatorPhase("config", "CONFIG_REFUSED", () => (deriveFinanceAgentProjectionV2(initialHostConfig, config.agentProfileV2, params.api.runtime.version, initialCodexPluginSource, initialOpenAiPluginSource)));
    const runner = dependencies.createRunner(config);
    const requireStableRuntimeEvidence = (params) => {
        if (params.currentArtifacts.openclaw.artifact_sha256 !== artifacts.openclaw.artifact_sha256 ||
            params.currentArtifacts.plugin.artifact_sha256 !== artifacts.plugin.artifact_sha256 ||
            params.currentArtifacts.plugin.source_identity_sha256 !==
                artifacts.plugin.source_identity_sha256 ||
            params.currentArtifacts.core.manifest_sha256 !== artifacts.core.manifest_sha256 ||
            params.currentArtifacts.core.wheel_sha256 !== artifacts.core.wheel_sha256 ||
            params.currentArtifacts.core.core_commit !== artifacts.core.core_commit ||
            params.currentArtifacts.core.api_contract_version !==
                artifacts.core.api_contract_version ||
            params.currentArtifacts.core.migration_ledger_digest !==
                artifacts.core.migration_ledger_digest ||
            params.currentArtifacts.finance_commit !== artifacts.finance_commit ||
            params.currentArtifacts.runtime_commit !== artifacts.runtime_commit ||
            canonicalProjectionSha256V2(params.currentProjection) !==
                canonicalProjectionSha256V2(projection)) {
            throw new Error(`Compatibility runtime evidence changed during ${params.phase}.`);
        }
    };
    let assets;
    let execution;
    try {
        execution = await executeReceiptBoundCompatibilityV1({
            async verifyBeforeProvider() {
                let health;
                try {
                    health = await runner.run(createBridgeRequest("health", { workspace_path: config.workspaceRoot }), 30_000);
                }
                catch (error) {
                    const category = bridgeProcessFailureCategory(error) ?? "BRIDGE_UNAVAILABLE";
                    throw createOperatorFailureV1(`Finance bridge pre-provider health refused: ${category}`, {
                        category: "BRIDGE_REFUSED",
                        phase: "health",
                        timer_layer: "none",
                        elapsed_ms: 0,
                        timeout_triggered: false,
                    });
                }
                if (health.status !== "ok" || health.result.workspace_verified !== true ||
                    health.result.database_verified !== true ||
                    health.result.callback_key_status !== "present") {
                    const category = health.status === "error" ? health.error.code : "INVALID_HEALTH_RESULT";
                    throw createOperatorFailureV1(`Finance bridge pre-provider health refused: ${category}`, {
                        category: "BRIDGE_REFUSED",
                        phase: "health",
                        timer_layer: "none",
                        elapsed_ms: 0,
                        timeout_triggered: false,
                    });
                }
                const preProviderArtifacts = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => await verifyArtifacts(evidenceParams));
                const preProviderHostRuntime = await runOperatorPhase("config", "CONFIG_REFUSED", async () => resolveReviewedFinanceHostRuntimeV2(params.api.runtime));
                await runOperatorPhase("config", "CONFIG_REFUSED", () => {
                    validateFinanceHostPolicyV2(preProviderHostRuntime.hostConfig);
                    validateReviewedPluginSourcesV2(preProviderHostRuntime.hostConfig, params.api.rootDir, openclawArtifactRoot, preProviderHostRuntime.codexPluginSource, preProviderHostRuntime.openaiPluginSource);
                });
                const preProviderProjection = await runOperatorPhase("config", "CONFIG_REFUSED", () => (deriveFinanceAgentProjectionV2(preProviderHostRuntime.hostConfig, config.agentProfileV2, params.api.runtime.version, preProviderHostRuntime.codexPluginSource, preProviderHostRuntime.openaiPluginSource)));
                try {
                    requireStableRuntimeEvidence({
                        currentArtifacts: preProviderArtifacts,
                        currentProjection: preProviderProjection,
                        phase: "pre-provider admission",
                    });
                }
                catch (error) {
                    throw asOperatorFailure(error, {
                        category: "ARTIFACT_REFUSED",
                        phase: "artifact",
                        timer_layer: "none",
                    });
                }
                await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => {
                    await verifyPlatform({
                        pluginRoot: params.api.rootDir,
                        artifact: preProviderArtifacts.plugin,
                        openclawArtifact: preProviderArtifacts.openclaw,
                    });
                });
                assets = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => (await loadCompatibilityAssetsV1(config.coreDistributionRoot)));
            },
            async runProviderCases() {
                if (assets === undefined) {
                    throw createOperatorFailureV1("Compatibility assets were not verified.", {
                        category: "ARTIFACT_REFUSED",
                        phase: "artifact",
                        timer_layer: "none",
                        elapsed_ms: 0,
                        timeout_triggered: false,
                    });
                }
                return await runHarness({
                    runtime: operatorRuntime(params.api),
                    runner,
                    projection,
                    assets,
                });
            },
            async verifyBeforeReceipt() {
                const finalArtifacts = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => await verifyArtifacts(evidenceParams));
                await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => {
                    await verifyPlatform({
                        pluginRoot: params.api.rootDir,
                        artifact: finalArtifacts.plugin,
                        openclawArtifact: finalArtifacts.openclaw,
                    });
                });
                const finalAssets = await runOperatorPhase("artifact", "ARTIFACT_REFUSED", async () => (await loadCompatibilityAssetsV1(config.coreDistributionRoot)));
                const finalHostRuntime = await runOperatorPhase("config", "CONFIG_REFUSED", async () => resolveReviewedFinanceHostRuntimeV2(params.api.runtime));
                const finalHostConfig = finalHostRuntime.hostConfig;
                const finalCodexPluginSource = finalHostRuntime.codexPluginSource;
                const finalOpenAiPluginSource = finalHostRuntime.openaiPluginSource;
                await runOperatorPhase("config", "CONFIG_REFUSED", () => {
                    validateFinanceHostPolicyV2(finalHostConfig);
                    validateReviewedPluginSourcesV2(finalHostConfig, params.api.rootDir, openclawArtifactRoot, finalCodexPluginSource, finalOpenAiPluginSource);
                });
                const finalProjection = await runOperatorPhase("config", "CONFIG_REFUSED", () => (deriveFinanceAgentProjectionV2(finalHostConfig, config.agentProfileV2, params.api.runtime.version, finalCodexPluginSource, finalOpenAiPluginSource)));
                try {
                    requireStableRuntimeEvidence({
                        currentArtifacts: finalArtifacts,
                        currentProjection: finalProjection,
                        phase: "pre-receipt admission",
                    });
                }
                catch (error) {
                    throw asOperatorFailure(error, {
                        category: "ARTIFACT_REFUSED",
                        phase: "artifact",
                        timer_layer: "none",
                    });
                }
                if (assets === undefined || finalAssets.identity_sha256 !== assets.identity_sha256) {
                    throw createOperatorFailureV1("Compatibility assets or runtime evidence changed during evaluation.", {
                        category: "ARTIFACT_REFUSED",
                        phase: "artifact",
                        timer_layer: "none",
                        elapsed_ms: 0,
                        timeout_triggered: false,
                    });
                }
            },
            async writeReceipt(outcomes) {
                return await registerCompatibilityReceiptV1({
                    runner,
                    workspaceRoot: config.workspaceRoot,
                    projection,
                    outcomes,
                });
            },
        });
    }
    catch (error) {
        throw asOperatorFailure(error, {
            category: "OPERATOR_REFUSED",
            phase: "unknown",
            timer_layer: "none",
        });
    }
    const outcomes = execution.outcomes;
    const receipt = execution.receipt;
    return {
        schema_version: "finance-model-compatibility-operator-result-v1",
        receipt,
        artifact_evidence: {
            policy_version: artifacts.openclaw.policy_version,
            openclaw_package_sha256: artifacts.openclaw.artifact_sha256,
            openclaw_file_count: artifacts.openclaw.file_count,
            plugin_build_sha256: artifacts.plugin.artifact_sha256,
            plugin_file_count: artifacts.plugin.file_count,
            plugin_source_identity_sha256: artifacts.plugin.source_identity_sha256,
            core_version: artifacts.core.core_version,
            core_manifest_sha256: artifacts.core.manifest_sha256,
            core_wheel_sha256: artifacts.core.wheel_sha256,
            core_api_contract_version: artifacts.core.api_contract_version,
            core_migration_ledger_digest: artifacts.core.migration_ledger_digest,
            finance_commit: artifacts.finance_commit,
            runtime_commit: artifacts.runtime_commit,
        },
        case_count: outcomes.length,
    };
}
export function registerOperatorCliV1(api, dependencies = defaultDependencies) {
    api.registerCli(({ program }) => {
        const root = program
            .command("finance-compatibility")
            .description("Run the bounded Finance model-compatibility operator workflow.");
        root
            .command("artifact-hash")
            .description("Compute one deterministic runtime artifact tree hash without model access.")
            .requiredOption("--kind <kind>", "openclaw_package, finance_plugin_build, or gate5_rehearsal_observer")
            .requiredOption("--root <absolute-path>", "absolute artifact root")
            .action(async (options) => {
            if (options.kind === "gate5_rehearsal_observer") {
                dependencies.writeOutput(await computeObserverHashV1(options.root));
                return;
            }
            if (options.kind !== "openclaw_package" && options.kind !== "finance_plugin_build") {
                throw new Error("Artifact kind is invalid.");
            }
            const result = await computeArtifactHashV1(options.kind, options.root);
            dependencies.writeOutput({
                policy_version: result.policy_version,
                artifact_kind: result.artifact_kind,
                package_version: result.package_version,
                artifact_sha256: result.artifact_sha256,
                file_count: result.file_count,
                byte_count: result.byte_count,
                source_identity_sha256: result.source_identity_sha256,
            });
        });
        root
            .command("register")
            .description("Run exactly four isolated cases and register one Python-verified receipt.")
            .action(async () => {
            try {
                const result = await executeCompatibilityRegistrationV1({
                    api,
                    dependencies,
                });
                dependencies.writeOutput(result);
            }
            catch (error) {
                dependencies.writeOutput(operatorFailureEnvelopeV1(error));
                throw new Error("Finance compatibility operator refused.");
            }
        });
    }, {
        commands: ["finance-compatibility"],
        descriptors: [{
                name: "finance-compatibility",
                description: "Bounded Finance model-compatibility operator workflow.",
                hasSubcommands: true,
            }],
    });
}
