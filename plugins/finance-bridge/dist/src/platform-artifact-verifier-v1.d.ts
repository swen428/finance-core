import type { ArtifactHashResultV1 } from "./artifact-hash-v1.js";
interface VerifierEnvironmentV1 {
    nodeVersion: string;
    platform: string;
    arch: string;
    verifyCodeSignature(path: string): void;
}
export interface PlatformArtifactEvidenceV1 {
    artifact_sha256: string;
    file_count: number;
    byte_count: number;
    compiled_runtime_sha256: string;
    source_identity_sha256: string;
}
export declare function verifyPlatformArtifactReceiptV1(params: {
    pluginRoot: string;
    artifact: ArtifactHashResultV1;
    openclawArtifact: ArtifactHashResultV1;
    environment?: Partial<VerifierEnvironmentV1>;
}): Promise<PlatformArtifactEvidenceV1>;
export declare function executeReceiptBoundCompatibilityV1<TOutcome, TReceipt>(params: {
    verifyBeforeProvider(): Promise<void>;
    runProviderCases(): Promise<TOutcome[]>;
    verifyBeforeReceipt(): Promise<void>;
    writeReceipt(outcomes: TOutcome[]): Promise<TReceipt>;
}): Promise<{
    outcomes: TOutcome[];
    receipt: TReceipt;
}>;
export {};
