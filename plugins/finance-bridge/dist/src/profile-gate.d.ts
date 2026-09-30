export declare const PROFILE_GATE_BASENAME = ".profile-gate.v1.lock";
declare const SHARED_LEASE_BRAND: unique symbol;
declare const EXCLUSIVE_LEASE_BRAND: unique symbol;
export interface SharedProfileGateLease {
    readonly [SHARED_LEASE_BRAND]: true;
    /** FD4 is a witness for Core; it is not an authorization path from JSON. */
    fdForChild(): number;
    /** Reserve this caller-owned lease before spawn; it cannot be closed while bound. */
    bindChild(): void;
    /** The runner calls this only after the child has closed and been reaped. */
    unbindChild(): void;
    /** Close-only release, after the caller's protected post-child work. */
    close(): void;
}
export interface ExclusiveProfileGateLease {
    readonly [EXCLUSIVE_LEASE_BRAND]: true;
    /** Cooperative hold bound, not preemption; exporters check every transition. */
    assertValid(): void;
    /** The delegated child inherits this description without reacquiring SH. */
    fdForChild(): number;
    /** Exactly one child may remain bound until actual close/reap. */
    reserveChild(): void;
    unbindChild(): void;
    close(): void;
}
export interface ProfileGate {
    acquireShared(timeoutMs: number): Promise<SharedProfileGateLease>;
    acquireExclusive(timeoutMs: number, maxHoldMs?: number): Promise<ExclusiveProfileGateLease>;
    close(): void;
}
export declare function isSharedProfileGateLease(value: unknown): value is SharedProfileGateLease;
export declare function isExclusiveProfileGateLease(value: unknown): value is ExclusiveProfileGateLease;
/**
 * The caller must first validate and pin the full profile/ancestor path boundary.
 * This entrypoint validates the final directory and lock; it does not establish
 * that an arbitrary mode-0700 directory is an authorized Finance profile.
 */
export declare function initializeProfileGate(profileRoot: string): void;
/** Open only an existing fixed lock within a caller-validated private profile. */
export declare function openProfileGate(profileRoot: string): ProfileGate;
export {};
