import type { ValidatedMedia } from "./media.js";
export declare const HANDOFF_PENDING_RECORD = ".finance-bridge.record.pending";
export declare const HANDOFF_PENDING_PAYLOAD = ".finance-bridge.payload.pending";
export type HandoffPhase = "after-lock" | "after-record-fsync" | "after-record-pin" | "after-record-publish" | "after-payload-fsync" | "after-payload-pin" | "after-payload-publish" | "before-callback";
export type HandoffHook = (phase: HandoffPhase) => void | Promise<void>;
export interface PublishedHandoff {
    handoffFilename: string;
    recordPath: string;
    payloadPath: string;
    rawIntakePublicId: string;
    contentHash: string;
}
interface PublisherOptions {
    hook?: HandoffHook;
    freeBytes?: (directoryFd: number) => Promise<number>;
    lockTimeoutMs?: number;
    markUnhealthy?: () => void;
}
export declare class HandoffPublisher {
    private readonly workspaceRoot;
    private readonly options;
    constructor(workspaceRoot: string, options?: PublisherOptions);
    publish(canonicalKey: string, rawIntakePublicId: string, media: ValidatedMedia): Promise<PublishedHandoff>;
    withRetained<T>(canonicalKey: string, rawIntakePublicId: string, callback: (published: PublishedHandoff, payloadFd: number, media: ValidatedMedia) => Promise<T>, lockTimeoutMs?: number): Promise<T | undefined>;
    withPublished<T>(canonicalKey: string, rawIntakePublicId: string, media: ValidatedMedia, callback: (published: PublishedHandoff, payloadFd: number) => Promise<T>, lockTimeoutMs?: number): Promise<T>;
    private publishLocked;
}
export {};
