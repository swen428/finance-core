import { closeSync, constants, existsSync, fchmodSync, fstatSync, fsyncSync, lstatSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
export const PROFILE_GATE_BASENAME = ".profile-gate.v1.lock";
const MAX_LOCK_WAIT_MS = 30_000;
const SHARED_LEASE_BRAND = Symbol("shared-profile-gate-lease");
const EXCLUSIVE_LEASE_BRAND = Symbol("exclusive-profile-gate-lease");
const liveSharedLeases = new WeakSet();
const liveExclusiveLeases = new WeakSet();
const requireFromModule = createRequire(import.meta.url);
let nativeGateFs;
let nativeFlock;
let nativeFlockSync;
function gateFs() {
    if (nativeGateFs === undefined) {
        const sourceDirectory = dirname(fileURLToPath(import.meta.url));
        const packageRoot = dirname(sourceDirectory).endsWith(`${join("", "dist")}`)
            ? dirname(dirname(sourceDirectory))
            : dirname(sourceDirectory);
        const binding = resolve(packageRoot, "build/Release/finance_bridge_posix.node");
        if (!existsSync(binding)) {
            throw new Error("Finance bridge POSIX boundary is not built for the pinned Node runtime.");
        }
        nativeGateFs = requireFromModule(binding);
    }
    return nativeGateFs;
}
function flockNonblocking(fd, operation, callback) {
    if (nativeFlock === undefined) {
        nativeFlock = requireFromModule("fs-ext").flock;
    }
    nativeFlock(fd, operation, callback);
}
function lockInitialization(fd) {
    if (nativeFlockSync === undefined) {
        nativeFlockSync = requireFromModule("fs-ext").flockSync;
    }
    nativeFlockSync(fd, "exnb");
}
export function isSharedProfileGateLease(value) {
    return typeof value === "object" && value !== null && liveSharedLeases.has(value);
}
export function isExclusiveProfileGateLease(value) {
    return typeof value === "object" && value !== null && liveExclusiveLeases.has(value);
}
function currentUid() {
    if (typeof process.getuid !== "function") {
        throw new Error("Profile gate requires POSIX owner identity.");
    }
    return process.getuid();
}
function sameIdentity(left, right) {
    return left.dev === right.dev && left.ino === right.ino;
}
function requirePrivateDirectory(fd) {
    const status = fstatSync(fd, { bigint: true });
    if (!status.isDirectory() || status.uid !== BigInt(currentUid()) ||
        (status.mode & 511n) !== 448n) {
        throw new Error("Profile gate directory must be owner-owned mode 0700.");
    }
    gateFs().rejectAclGrants(fd);
    return { dev: status.dev, ino: status.ino };
}
function requirePrivateLock(fd) {
    const status = fstatSync(fd, { bigint: true });
    if (!status.isFile() || status.uid !== BigInt(currentUid()) ||
        (status.mode & 511n) !== 384n || status.nlink !== 1n || status.size !== 0n) {
        throw new Error("Profile gate lock must be a private empty single-link regular file.");
    }
    gateFs().rejectAclGrants(fd);
    return { dev: status.dev, ino: status.ino };
}
function openValidatedRoot(profileRoot) {
    if (typeof profileRoot !== "string" || !isAbsolute(profileRoot) ||
        resolve(profileRoot) !== profileRoot || realpathSync(profileRoot) !== profileRoot ||
        lstatSync(profileRoot).isSymbolicLink()) {
        throw new Error("Profile gate root must be an explicit canonical directory.");
    }
    const fd = gateFs().openDirectory(profileRoot);
    try {
        return { fd, identity: requirePrivateDirectory(fd) };
    }
    catch (error) {
        closeSync(fd);
        throw error;
    }
}
function requireCurrentRoot(profileRoot, rootIdentity) {
    const fresh = openValidatedRoot(profileRoot);
    try {
        if (!sameIdentity(fresh.identity, rootIdentity)) {
            throw new Error("Profile gate directory identity changed.");
        }
    }
    finally {
        closeSync(fresh.fd);
    }
}
function openValidatedLock(rootFd, expected) {
    const fd = gateFs().openFileAt(rootFd, PROFILE_GATE_BASENAME, constants.O_RDWR | constants.O_NONBLOCK, 0);
    try {
        const actual = requirePrivateLock(fd);
        if (expected !== undefined && !sameIdentity(actual, expected)) {
            throw new Error("Profile gate lock identity changed.");
        }
        return { fd, identity: actual };
    }
    catch (error) {
        closeSync(fd);
        throw error;
    }
}
function tryLock(fd, operation) {
    return new Promise((resolvePromise, reject) => {
        flockNonblocking(fd, operation, (error) => {
            if (error === null || error === undefined)
                return resolvePromise(true);
            if (error.code === "EAGAIN" || error.code === "EWOULDBLOCK") {
                return resolvePromise(false);
            }
            reject(error);
        });
    });
}
function sharedLease(fd, onClose) {
    let closed = false;
    let bound = false;
    const requireOpen = () => {
        if (closed)
            throw new Error("Profile gate lease is closed.");
    };
    const lease = Object.freeze({
        [SHARED_LEASE_BRAND]: true,
        fdForChild() {
            requireOpen();
            return fd;
        },
        bindChild() {
            requireOpen();
            if (bound)
                throw new Error("Profile gate lease is already bound to a child.");
            bound = true;
        },
        unbindChild() {
            requireOpen();
            if (!bound)
                throw new Error("Profile gate lease has no bound child.");
            bound = false;
        },
        close() {
            if (closed)
                return;
            if (bound)
                throw new Error("Profile gate lease remains bound until child reap.");
            closeSync(fd);
            closed = true;
            liveSharedLeases.delete(lease);
            onClose();
        },
    });
    liveSharedLeases.add(lease);
    return lease;
}
function exclusiveLease(fd, holdDeadline, validate, onClose) {
    let closed = false;
    let bound = false;
    const close = () => {
        if (closed)
            return;
        if (bound)
            throw new Error("Exclusive profile gate remains bound until child reap.");
        closeSync(fd);
        closed = true;
        liveExclusiveLeases.delete(lease);
        onClose();
    };
    const checkHoldDeadline = () => {
        if (performance.now() >= holdDeadline) {
            throw new Error("Exclusive profile gate hold deadline exceeded.");
        }
    };
    const lease = Object.freeze({
        [EXCLUSIVE_LEASE_BRAND]: true,
        assertValid() {
            if (closed)
                throw new Error("Exclusive profile gate lease is closed.");
            checkHoldDeadline();
            validate();
            checkHoldDeadline();
        },
        fdForChild() {
            if (closed || !bound)
                throw new Error("Exclusive profile gate has no reserved child.");
            return fd;
        },
        reserveChild() {
            if (closed || bound)
                throw new Error("Exclusive profile gate cannot reserve another child.");
            checkHoldDeadline();
            validate();
            checkHoldDeadline();
            bound = true;
        },
        unbindChild() {
            if (closed || !bound)
                throw new Error("Exclusive profile gate has no reserved child.");
            bound = false;
        },
        close,
    });
    liveExclusiveLeases.add(lease);
    return lease;
}
/**
 * The caller must first validate and pin the full profile/ancestor path boundary.
 * This entrypoint validates the final directory and lock; it does not establish
 * that an arbitrary mode-0700 directory is an authorized Finance profile.
 */
export function initializeProfileGate(profileRoot) {
    const root = openValidatedRoot(profileRoot);
    try {
        const fd = gateFs().openFileAt(root.fd, PROFILE_GATE_BASENAME, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR, 0o000);
        let initialized = false;
        try {
            lockInitialization(fd);
            fchmodSync(fd, 0o600);
            const created = requirePrivateLock(fd);
            requireCurrentRoot(profileRoot, root.identity);
            const current = openValidatedLock(root.fd, created);
            closeSync(current.fd);
            fsyncSync(fd);
            fsyncSync(root.fd);
            requireCurrentRoot(profileRoot, root.identity);
            const afterSync = openValidatedLock(root.fd, created);
            closeSync(afterSync.fd);
            initialized = true;
        }
        finally {
            if (!initialized) {
                try {
                    fchmodSync(fd, 0o000);
                    fsyncSync(fd);
                    fsyncSync(root.fd);
                }
                catch {
                    // Preserve the original failure; the file remains for explicit owner repair.
                }
            }
            closeSync(fd);
        }
    }
    finally {
        closeSync(root.fd);
    }
}
/** Open only an existing fixed lock within a caller-validated private profile. */
export function openProfileGate(profileRoot) {
    const root = openValidatedRoot(profileRoot);
    let lock;
    try {
        lock = openValidatedLock(root.fd);
        requireCurrentRoot(profileRoot, root.identity);
    }
    catch (error) {
        closeSync(root.fd);
        throw error;
    }
    closeSync(lock.fd);
    let closed = false;
    let activeLeases = 0;
    async function acquire(operation, timeoutMs, maxHoldMs = MAX_LOCK_WAIT_MS) {
        if (closed)
            throw new Error("Profile gate is closed.");
        if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_LOCK_WAIT_MS) {
            throw new Error("Profile gate wait must be an integer from 1 through 30000 milliseconds.");
        }
        if (operation === "exnb" &&
            (!Number.isSafeInteger(maxHoldMs) || maxHoldMs <= 0 || maxHoldMs > MAX_LOCK_WAIT_MS)) {
            throw new Error("Exclusive profile gate hold must be an integer from 1 through 30000 milliseconds.");
        }
        const deadline = performance.now() + timeoutMs;
        requireCurrentRoot(profileRoot, root.identity);
        const opened = openValidatedLock(root.fd, lock.identity);
        let transferred = false;
        try {
            for (;;) {
                if (closed)
                    throw new Error("Profile gate closed during acquisition.");
                // fs-ext may obtain the lock before its callback reaches this task.
                const attemptStarted = operation === "exnb" ? performance.now() : undefined;
                if (await tryLock(opened.fd, operation)) {
                    const holdDeadline = attemptStarted === undefined ? undefined : attemptStarted + maxHoldMs;
                    if (performance.now() > deadline) {
                        throw new Error("Profile gate wait deadline exceeded.");
                    }
                    const onClose = () => { activeLeases -= 1; };
                    const validate = () => {
                        requirePrivateLock(opened.fd);
                        requireCurrentRoot(profileRoot, root.identity);
                        const named = openValidatedLock(root.fd, lock.identity);
                        closeSync(named.fd);
                    };
                    validate();
                    if (holdDeadline !== undefined && performance.now() >= holdDeadline) {
                        throw new Error("Exclusive profile gate hold deadline exceeded.");
                    }
                    const lease = operation === "shnb"
                        ? sharedLease(opened.fd, onClose)
                        : exclusiveLease(opened.fd, holdDeadline, validate, onClose);
                    activeLeases += 1;
                    transferred = true;
                    return lease;
                }
                const remaining = deadline - performance.now();
                if (remaining <= 0)
                    throw new Error("Profile gate wait deadline exceeded.");
                await delay(Math.min(25, Math.ceil(remaining)));
            }
        }
        finally {
            if (!transferred)
                closeSync(opened.fd);
        }
    }
    return Object.freeze({
        acquireShared(timeoutMs) {
            return acquire("shnb", timeoutMs);
        },
        acquireExclusive(timeoutMs, maxHoldMs) {
            return acquire("exnb", timeoutMs, maxHoldMs);
        },
        close() {
            if (closed)
                return;
            if (activeLeases !== 0)
                throw new Error("Profile gate still has active leases.");
            closeSync(root.fd);
            closed = true;
        },
    });
}
