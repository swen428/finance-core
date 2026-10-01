import { type ProfileRootLocator } from "./profile-layout.js";
import { type DescriptorIdentity } from "./posix.js";
export interface BridgeProfileLocator extends ProfileRootLocator {
    readonly runtimeRoot: string;
}
export interface BridgeCutContext {
    readonly profileId: string;
    readonly cutId: string;
    readonly workspaceRoot: string;
    readonly handoffRoot: string;
    readonly stageRelativeName: string;
    readonly stagePath: string;
}
export interface BridgeStageWrite {
    readonly relativeName: string;
    readonly byteSize: number;
    readonly sha256: string;
}
export interface PrivateBridgeStageSink {
    readonly stagePath: string;
    writeValidated(relativeName: string, bytes: Buffer): Promise<BridgeStageWrite>;
}
export interface BridgeSourceIdentity {
    readonly fd: number;
    readonly identity: DescriptorIdentity;
}
export interface BridgeCutOptions {
    readonly waitMs?: number;
    readonly maxHoldMs?: number;
}
/** Check the live, internally acquired cut and profile pins before/after source I/O. */
export declare function assertBridgeCut(context: BridgeCutContext, sink: PrivateBridgeStageSink, source?: BridgeSourceIdentity): void;
/** Owns the exclusive lock; neither an FD nor a caller flag can supply its authority. */
export declare function withExclusiveBridgeCut<T>(locator: BridgeProfileLocator, callback: (context: BridgeCutContext, sink: PrivateBridgeStageSink) => Promise<T>, options?: BridgeCutOptions): Promise<T>;
