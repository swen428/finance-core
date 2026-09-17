import type { FinanceBridgeConfig } from "./config.js";
export interface CoreDistributionEvidenceV1 {
    schema: "finance-core-distribution-proof-v1";
    core_version: string;
    core_commit: string;
    manifest_sha256: string;
    wheel_sha256: string;
    api_contract_version: string;
    migration_ledger_digest: string;
}
export declare function verifyCoreDistributionV1(config: FinanceBridgeConfig): Promise<CoreDistributionEvidenceV1>;
