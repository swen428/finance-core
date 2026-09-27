import { closeSync, constants, existsSync, fchmodSync, fstatSync, fsyncSync, lstatSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

export const PROFILE_GATE_BASENAME = ".profile-gate.v1.lock";
const MAX_LOCK_WAIT_MS = 30_000;
const SHARED_LEASE_BRAND: unique symbol = Symbol("shared-profile-gate-lease");
const liveSharedLeases = new WeakSet<object>();
const requireFromModule = createRequire(import.meta.url);

interface NativeGateFs {
  openDirectory(path: string): number;
  openFileAt(directoryFd: number, name: string, flags: number, mode: number): number;
  rejectAclGrants(fd: number): void;
}

type Flock = (
  fd: number,
  operation: "shnb" | "exnb",
  callback: (error?: NodeJS.ErrnoException | null) => void,
) => void;
type FlockSync = (fd: number, operation: "exnb") => void;

let nativeGateFs: NativeGateFs | undefined;
let nativeFlock: Flock | undefined;
let nativeFlockSync: FlockSync | undefined;

function gateFs(): NativeGateFs {
  if (nativeGateFs === undefined) {
    const sourceDirectory = dirname(fileURLToPath(import.meta.url));
    const packageRoot = dirname(sourceDirectory).endsWith(`${join("", "dist")}`)
      ? dirname(dirname(sourceDirectory))
      : dirname(sourceDirectory);
    const binding = resolve(packageRoot, "build/Release/finance_bridge_posix.node");
    if (!existsSync(binding)) {
      throw new Error("Finance bridge POSIX boundary is not built for the pinned Node runtime.");
    }
    nativeGateFs = requireFromModule(binding) as NativeGateFs;
  }
  return nativeGateFs;
}

function flockNonblocking(fd: number, operation: "shnb" | "exnb", callback: Parameters<Flock>[2]): void {
  if (nativeFlock === undefined) {
    nativeFlock = (requireFromModule("fs-ext") as { flock: Flock }).flock;
  }
  nativeFlock(fd, operation, callback);
}

function lockInitialization(fd: number): void {
  if (nativeFlockSync === undefined) {
    nativeFlockSync = (requireFromModule("fs-ext") as { flockSync: FlockSync }).flockSync;
  }
  nativeFlockSync(fd, "exnb");
}

interface EntryIdentity {
  dev: bigint;
  ino: bigint;
}

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
  /** Cooperative hold bound, not preemption; exporters check every transition. */
  assertValid(): void;
  close(): void;
}

export interface ProfileGate {
  acquireShared(timeoutMs: number): Promise<SharedProfileGateLease>;
  acquireExclusive(timeoutMs: number, maxHoldMs?: number): Promise<ExclusiveProfileGateLease>;
  close(): void;
}

export function isSharedProfileGateLease(value: unknown): value is SharedProfileGateLease {
  return typeof value === "object" && value !== null && liveSharedLeases.has(value);
}

function currentUid(): number {
  if (typeof process.getuid !== "function") {
    throw new Error("Profile gate requires POSIX owner identity.");
  }
  return process.getuid();
}

function sameIdentity(left: EntryIdentity, right: EntryIdentity): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

function requirePrivateDirectory(fd: number): EntryIdentity {
  const status = fstatSync(fd, { bigint: true });
  if (!status.isDirectory() || status.uid !== BigInt(currentUid()) ||
      (status.mode & 0o777n) !== 0o700n) {
    throw new Error("Profile gate directory must be owner-owned mode 0700.");
  }
  gateFs().rejectAclGrants(fd);
  return { dev: status.dev, ino: status.ino };
}

function requirePrivateLock(fd: number): EntryIdentity {
  const status = fstatSync(fd, { bigint: true });
  if (!status.isFile() || status.uid !== BigInt(currentUid()) ||
      (status.mode & 0o777n) !== 0o600n || status.nlink !== 1n || status.size !== 0n) {
    throw new Error("Profile gate lock must be a private empty single-link regular file.");
  }
  gateFs().rejectAclGrants(fd);
  return { dev: status.dev, ino: status.ino };
}

function openValidatedRoot(profileRoot: string): { fd: number; identity: EntryIdentity } {
  if (typeof profileRoot !== "string" || !isAbsolute(profileRoot) ||
      resolve(profileRoot) !== profileRoot || realpathSync(profileRoot) !== profileRoot ||
      lstatSync(profileRoot).isSymbolicLink()) {
    throw new Error("Profile gate root must be an explicit canonical directory.");
  }
  const fd = gateFs().openDirectory(profileRoot);
  try {
    return { fd, identity: requirePrivateDirectory(fd) };
  } catch (error) {
    closeSync(fd);
    throw error;
  }
}

function requireCurrentRoot(profileRoot: string, rootIdentity: EntryIdentity): void {
  const fresh = openValidatedRoot(profileRoot);
  try {
    if (!sameIdentity(fresh.identity, rootIdentity)) {
      throw new Error("Profile gate directory identity changed.");
    }
  } finally {
    closeSync(fresh.fd);
  }
}

function openValidatedLock(rootFd: number, expected?: EntryIdentity): { fd: number; identity: EntryIdentity } {
  const fd = gateFs().openFileAt(rootFd, PROFILE_GATE_BASENAME, constants.O_RDWR | constants.O_NONBLOCK, 0);
  try {
    const actual = requirePrivateLock(fd);
    if (expected !== undefined && !sameIdentity(actual, expected)) {
      throw new Error("Profile gate lock identity changed.");
    }
    return { fd, identity: actual };
  } catch (error) {
    closeSync(fd);
    throw error;
  }
}

function tryLock(fd: number, operation: "shnb" | "exnb"): Promise<boolean> {
  return new Promise((resolvePromise, reject) => {
    flockNonblocking(fd, operation, (error) => {
      if (error === null || error === undefined) return resolvePromise(true);
      if (error.code === "EAGAIN" || error.code === "EWOULDBLOCK") {
        return resolvePromise(false);
      }
      reject(error);
    });
  });
}

function sharedLease(fd: number, onClose: () => void): SharedProfileGateLease {
  let closed = false;
  let bound = false;
  const requireOpen = (): void => {
    if (closed) throw new Error("Profile gate lease is closed.");
  };
  const lease: SharedProfileGateLease = Object.freeze({
    [SHARED_LEASE_BRAND]: true as const,
    fdForChild(): number {
      requireOpen();
      return fd;
    },
    bindChild(): void {
      requireOpen();
      if (bound) throw new Error("Profile gate lease is already bound to a child.");
      bound = true;
    },
    unbindChild(): void {
      requireOpen();
      if (!bound) throw new Error("Profile gate lease has no bound child.");
      bound = false;
    },
    close(): void {
      if (closed) return;
      if (bound) throw new Error("Profile gate lease remains bound until child reap.");
      closeSync(fd);
      closed = true;
      liveSharedLeases.delete(lease);
      onClose();
    },
  });
  liveSharedLeases.add(lease);
  return lease;
}

function exclusiveLease(
  fd: number,
  maxHoldMs: number,
  validate: () => void,
  onClose: () => void,
): ExclusiveProfileGateLease {
  let closed = false;
  const holdDeadline = performance.now() + maxHoldMs;
  return Object.freeze({
    assertValid(): void {
      if (closed) throw new Error("Exclusive profile gate lease is closed.");
      if (performance.now() >= holdDeadline) {
        throw new Error("Exclusive profile gate hold deadline exceeded.");
      }
      validate();
    },
    close(): void {
      if (closed) return;
      closeSync(fd);
      closed = true;
      onClose();
    },
  });
}

/**
 * The caller must first validate and pin the full profile/ancestor path boundary.
 * This entrypoint validates the final directory and lock; it does not establish
 * that an arbitrary mode-0700 directory is an authorized Finance profile.
 */
export function initializeProfileGate(profileRoot: string): void {
  const root = openValidatedRoot(profileRoot);
  try {
    const fd = gateFs().openFileAt(
      root.fd,
      PROFILE_GATE_BASENAME,
      constants.O_CREAT | constants.O_EXCL | constants.O_RDWR,
      0o000,
    );
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
      initialized = true;
    } finally {
      if (!initialized) {
        try {
          fchmodSync(fd, 0o000);
          fsyncSync(fd);
          fsyncSync(root.fd);
        } catch {
          // Preserve the original failure; the file remains for explicit owner repair.
        }
      }
      closeSync(fd);
    }
  } finally {
    closeSync(root.fd);
  }
}

/** Open only an existing fixed lock within a caller-validated private profile. */
export function openProfileGate(profileRoot: string): ProfileGate {
  const root = openValidatedRoot(profileRoot);
  let lock: { fd: number; identity: EntryIdentity };
  try {
    lock = openValidatedLock(root.fd);
    requireCurrentRoot(profileRoot, root.identity);
  } catch (error) {
    closeSync(root.fd);
    throw error;
  }
  closeSync(lock.fd);
  let closed = false;
  let activeLeases = 0;

  async function acquire(
    operation: "shnb" | "exnb",
    timeoutMs: number,
    maxHoldMs = MAX_LOCK_WAIT_MS,
  ): Promise<SharedProfileGateLease | ExclusiveProfileGateLease> {
    if (closed) throw new Error("Profile gate is closed.");
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
        if (closed) throw new Error("Profile gate closed during acquisition.");
        if (await tryLock(opened.fd, operation)) {
          if (performance.now() > deadline) {
            throw new Error("Profile gate wait deadline exceeded.");
          }
          requireCurrentRoot(profileRoot, root.identity);
          const current = openValidatedLock(root.fd, lock.identity);
          closeSync(current.fd);
          const onClose = (): void => { activeLeases -= 1; };
          const validate = (): void => {
            requirePrivateLock(opened.fd);
            requireCurrentRoot(profileRoot, root.identity);
            const named = openValidatedLock(root.fd, lock.identity);
            closeSync(named.fd);
          };
          const lease = operation === "shnb"
            ? sharedLease(opened.fd, onClose)
            : exclusiveLease(opened.fd, maxHoldMs, validate, onClose);
          activeLeases += 1;
          transferred = true;
          return lease;
        }
        const remaining = deadline - performance.now();
        if (remaining <= 0) throw new Error("Profile gate wait deadline exceeded.");
        await delay(Math.min(25, Math.ceil(remaining)));
      }
    } finally {
      if (!transferred) closeSync(opened.fd);
    }
  }

  return Object.freeze({
    acquireShared(timeoutMs: number): Promise<SharedProfileGateLease> {
      return acquire("shnb", timeoutMs) as Promise<SharedProfileGateLease>;
    },
    acquireExclusive(timeoutMs: number, maxHoldMs?: number): Promise<ExclusiveProfileGateLease> {
      return acquire("exnb", timeoutMs, maxHoldMs) as Promise<ExclusiveProfileGateLease>;
    },
    close(): void {
      if (closed) return;
      if (activeLeases !== 0) throw new Error("Profile gate still has active leases.");
      closeSync(root.fd);
      closed = true;
    },
  });
}
