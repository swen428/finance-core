export interface FinanceBridgeConfig {
    repoRoot: string;
    coreDistributionRoot: string;
    pythonExecutable: string;
    workspaceRoot: string;
    agentProfileV2: FinanceAgentProfileEvidenceV2;
}
export interface FinanceAgentProfileEvidenceV2 {
    openclawPackageSha256: string;
    financeCommit: string;
    coreVersion: string;
    coreManifestSha256: string;
    coreWheelSha256: string;
    coreApiContractVersion: string;
    coreMigrationLedgerDigest: string;
    pluginBuildSha256: string;
    executionClass: "local_model" | "cloud_projection";
}
export declare function revalidatePythonExecutableForSpawn(config: FinanceBridgeConfig): Promise<void>;
export declare function validatePluginConfig(value: unknown): Promise<FinanceBridgeConfig>;
