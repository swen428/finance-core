import { type FinanceBridgeConfig } from "./config.js";
export interface ManagedCoreSnapshotLimits {
    readonly maxCoreDbBytes: number;
    readonly maxStageBytes: number;
    readonly minFreeBytes: number;
    readonly backupPagesPerStep: number;
}
export interface ManagedCoreSnapshotOptions {
    readonly config: FinanceBridgeConfig;
    readonly applicationSupportRoot: string;
    readonly profileId: string;
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
/** Component-only: one EX covers both fixed children and their actual reap. */
export declare function runManagedCoreSnapshot(options: ManagedCoreSnapshotOptions): Promise<ManagedCoreSnapshotReceipt>;
