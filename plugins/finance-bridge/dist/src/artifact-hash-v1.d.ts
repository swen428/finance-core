export declare const ARTIFACT_HASH_POLICY_VERSION = "finance-runtime-artifact-tree-v1";
export declare const BUILD_SOURCE_POLICY_VERSION = "finance-plugin-build-source-v3";
export declare const OBSERVER_SOURCE_POLICY_VERSION = "finance-gate5-rehearsal-observer-source-v1";
export declare const OBSERVER_SOURCE_VERSION = "finance-gate5-rehearsal-observer-v1";
export type ArtifactKind = "openclaw_package" | "finance_plugin_build";
export interface ArtifactHashEntryV1 {
    path: string;
    byte_count: number;
    mode: number;
    sha256: string;
}
export interface ArtifactHashResultV1 {
    policy_version: typeof ARTIFACT_HASH_POLICY_VERSION;
    artifact_kind: ArtifactKind;
    package_version: string;
    artifact_sha256: string;
    file_count: number;
    byte_count: number;
    source_identity_sha256: string | null;
    entries: ArtifactHashEntryV1[];
}
export interface BuildSourceIdentityV1 {
    policy_version: typeof BUILD_SOURCE_POLICY_VERSION;
    source_identity_sha256: string;
    file_count: number;
    byte_count: number;
    entries: ArtifactHashEntryV1[];
}
export interface ObserverHashResultV1 {
    policy_version: typeof OBSERVER_SOURCE_POLICY_VERSION;
    observer_version: typeof OBSERVER_SOURCE_VERSION;
    observer_sha256: string;
    file_count: number;
    byte_count: number;
    entries: ArtifactHashEntryV1[];
}
export declare const OBSERVER_SOURCE_PATHS: readonly ["scripts/run-gate5-local-rehearsal.mjs", "scripts/gate5-loopback-only.sb"];
export declare function computeBuildSourceIdentityV1(rootValue: string): Promise<BuildSourceIdentityV1>;
export declare function computeObserverHashV1(rootValue: string): Promise<ObserverHashResultV1>;
export declare function resolveOpenClawArtifactRootV1(argv1Value: string): Promise<string>;
export declare function computeArtifactHashV1(kind: ArtifactKind, rootValue: string): Promise<ArtifactHashResultV1>;
