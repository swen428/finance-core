import { type FinanceBridgeConfig } from "./config.js";
import { type ProfileRootLocator } from "./profile-layout.js";
export interface ManagedCoreSnapshotLimits {
    readonly maxCoreDbBytes: number;
    readonly maxStageBytes: number;
    readonly minFreeBytes: number;
    readonly backupPagesPerStep: number;
}
export interface ManagedCoreSnapshotOptions extends ProfileRootLocator {
    readonly config: FinanceBridgeConfig;
    readonly limits: ManagedCoreSnapshotLimits;
    readonly waitMs?: number;
    readonly maxHoldMs?: number;
    readonly signal?: AbortSignal;
}
export interface ManagedCoreSnapshotReceipt {
    readonly cutId: string;
    readonly stagePath: string;
    readonly byteLength: number;
    readonly sha256: string;
    readonly pageCount: number;
    readonly schemaObjectCount: number;
    readonly journalMode: "delete";
}
export interface ManagedCoreSnapshotBundleOptions extends ProfileRootLocator {
    readonly config: FinanceBridgeConfig;
    readonly waitMs?: number;
    readonly maxHoldMs?: number;
    readonly signal?: AbortSignal;
}
export interface ManagedCoreSnapshotBundleReceipt {
    readonly version: "core-snapshot-bundle-receipt-v1";
    readonly scope: "core_committed_snapshot";
    readonly status: "snapshot_verified";
    readonly cutId: string;
    readonly stagePath: string;
    readonly manifestSha256: string;
    readonly manifestByteLength: number;
    readonly dbSha256: string;
    readonly dbByteLength: number;
    readonly memberCount: number;
    readonly referenceCount: number;
    readonly memberBytes: number;
    readonly referenceDigest: string;
    readonly snapshotPoint: string;
    readonly packageCompletedAt: string;
    readonly verifiedAt: string;
}
/** Component-only: one EX covers both fixed children and their actual reap. */
export declare function runManagedCoreSnapshot(options: ManagedCoreSnapshotOptions): Promise<ManagedCoreSnapshotReceipt>;
/** Fixed Core committed snapshot bundle; EX ends only after final Node tree verification. */
export declare function runManagedCoreSnapshotBundle(options: ManagedCoreSnapshotBundleOptions): Promise<ManagedCoreSnapshotBundleReceipt>;
