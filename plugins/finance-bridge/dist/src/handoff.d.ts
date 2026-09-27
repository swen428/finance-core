import type { ValidatedMedia } from "./media.js";
import { type BridgeCutContext, type PrivateBridgeStageSink } from "./bridge-export-boundary.js";
export declare const HANDOFF_PENDING_RECORD = ".finance-bridge.record.pending";
export declare const HANDOFF_PENDING_PAYLOAD = ".finance-bridge.payload.pending";
export type HandoffPhase = "after-lock" | "after-record-fsync" | "after-record-pin" | "after-record-publish" | "after-payload-fsync" | "after-payload-pin" | "after-payload-publish" | "before-callback" | "after-reclaim-payload-unlink" | "after-reclaim-payload-fsync" | "after-reclaim-record-unlink" | "after-reclaim-record-fsync" | "after-reclaim-intent-unlink" | "after-reclaim-intent-fsync";
export type HandoffHook = (phase: HandoffPhase) => void | Promise<void>;
/** A claim identifies one Core-owned original; it does not itself prove custody. */
export interface ReclaimClaim {
    rawIntakePublicId: string;
    jobPublicId: string;
    canonicalKeyHash: string;
    ingressIdentityDigest: string;
    attachmentContentHash: string;
    intakeFingerprint: string;
}
export type HandoffClaimState = "clear" | "incomplete" | "retained" | "reclaiming";
/** Core evidence changed while an inbound photo was waiting for the publication flock. */
export declare class HandoffPublicationRefused extends Error {
}
export interface PublishedHandoff {
    handoffFilename: string;
    recordPath: string;
    payloadPath: string;
    rawIntakePublicId: string;
    contentHash: string;
}
export interface FrozenBridgeFileV1 {
    role: "record" | "payload" | "reclaim_intent" | "pending_record" | "pending_payload";
    rawIntakePublicId: string | null;
    sourceName: string;
    frozenName: string;
    byteSize: number;
    sha256: string;
}
export interface FrozenBridgeSlotV1 {
    rawIntakePublicId: string;
    canonicalKeyHash: string;
    attachmentSha256: string;
    byteSize: number;
    mimeType: "image/jpeg" | "image/png";
    state: "retained" | "reclaiming" | "incomplete";
    coreCustodyRequired: true;
}
export interface FrozenBridgeHandoffV1 {
    contractVersion: "finance-bridge-handoff-export-v1";
    owner: "finance-bridge";
    profileId: string;
    cutId: string;
    files: FrozenBridgeFileV1[];
    slots: FrozenBridgeSlotV1[];
    pendingPublicationKeyHash: string | null;
    totalBytes: number;
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
    /** Freeze only Bridge-owned handoff bytes under the caller's live exclusive cut. */
    exportFrozen(cut: BridgeCutContext, sink: PrivateBridgeStageSink): Promise<FrozenBridgeHandoffV1>;
    private withReclaimLock;
    /** Lists only durable intents. The caller must query Core outside the flock. */
    pendingReclaims(): Promise<ReclaimClaim[]>;
    /** A pending publication can only resume for its original trusted message. */
    pendingPublicationKeyHash(): Promise<string | undefined>;
    /** A host replay can seal an older retained slot after Core commit lost its response. */
    prepareRetainedReclaim(canonicalKey: string, claim: ReclaimClaim): Promise<boolean>;
    isReclaimed(claim: ReclaimClaim): Promise<boolean>;
    /** One flock snapshot covers pending names, the final record, payload and intent. */
    claimState(claim: ReclaimClaim): Promise<HandoffClaimState>;
    /** Proof is obtained from Core outside the flock before *every* cleanup attempt. */
    reclaimVerified(claim: ReclaimClaim, proveCoreCustody: (claim: ReclaimClaim) => Promise<boolean>, deadlineAt?: number): Promise<boolean>;
    private verifyReclaimInventory;
    publish(canonicalKey: string, rawIntakePublicId: string, media: ValidatedMedia): Promise<PublishedHandoff>;
    withRetained<T>(canonicalKey: string, rawIntakePublicId: string, callback: (published: PublishedHandoff, payloadFd: number, media: ValidatedMedia) => Promise<T>, lockTimeoutMs?: number, reclaimClaim?: ReclaimClaim): Promise<T | undefined>;
    withPublished<T>(canonicalKey: string, rawIntakePublicId: string, media: ValidatedMedia, callback: (published: PublishedHandoff, payloadFd: number) => Promise<T>, lockTimeoutMs?: number, reclaimClaim?: ReclaimClaim, beforePublication?: (hasLocalSlot: boolean) => Promise<boolean>): Promise<T>;
    private publishLocked;
}
export {};
