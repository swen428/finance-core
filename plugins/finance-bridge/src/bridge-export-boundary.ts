/** A short, locally held profile cut and private stage for an owner-only exporter. */
import { randomBytes, createHash } from "node:crypto";
import { closeSync, constants, fstatSync, fsyncSync, lstatSync, readSync, realpathSync, writeSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";

import { openProfileGate, type ExclusiveProfileGateLease, type ProfileGate } from "./profile-gate.js";
import {
  createDirectoryExclusiveAt, descriptorIdentitySync, openDirectory,
  openExistingDirectoryAt, openFileAt, listAt, rejectAclGrants, type DescriptorIdentity,
} from "./posix.js";

const PROFILE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/u;
const STAGE_PREFIX = "owner-export-";
const MAX_MANIFEST_BYTES = 65_536;
const MAX_STAGE_FILE_BYTES = 321_000_000;
const MAX_STAGE_TOTAL_BYTES = 322_000_000;

export interface BridgeProfileLocator {
  readonly applicationSupportRoot: string;
  readonly profileId: string;
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

type Pin = { readonly path: string; readonly fd: number; readonly directory: boolean; readonly dev: bigint; readonly ino: bigint };
type VerifiedOutput = { readonly relativeName: string; readonly fd: number; readonly dev: bigint; readonly ino: bigint; readonly byteSize: number; readonly sha256: string };
type ActiveCut = {
  readonly context: BridgeCutContext;
  readonly sink: PrivateBridgeStageSink;
  readonly pins: Pin[];
  readonly absentFiles: string[];
  readonly stageFd: number;
  readonly stagePath: string;
  readonly gate: ProfileGate;
  readonly lease: ExclusiveProfileGateLease;
  readonly outputFds: number[];
  readonly verifiedOutputs: VerifiedOutput[];
  readonly locator: BridgeProfileLocator;
  active: boolean;
};
const activeContexts = new WeakMap<object, ActiveCut>();
const activeSinks = new WeakMap<object, ActiveCut>();

function uid(): bigint {
  if (typeof process.getuid !== "function") throw new Error("Bridge profile requires POSIX owner identity.");
  return BigInt(process.getuid());
}

function canonicalPath(path: string): void {
  if (typeof path !== "string" || !isAbsolute(path) || resolve(path) !== path ||
      realpathSync(path) !== path || lstatSync(path).isSymbolicLink()) {
    throw new Error("Bridge profile path must be absolute and canonical.");
  }
}

function checkAncestors(applicationSupportRoot: string): void {
  canonicalPath(applicationSupportRoot);
  if (basename(applicationSupportRoot) !== "Application Support") {
    throw new Error("Explicit Application Support root is required.");
  }
  for (let path = applicationSupportRoot;; path = dirname(path)) {
    const named = lstatSync(path, { bigint: true });
    if (!named.isDirectory() || named.isSymbolicLink() ||
        (named.uid !== 0n && named.uid !== uid()) ||
        (((named.mode & 0o022n) !== 0n) && !(named.uid === 0n && (named.mode & 0o1000n) !== 0n))) {
      throw new Error("Unsafe Application Support ancestor.");
    }
    const fd = openDirectory(path);
    try {
      const opened = fstatSync(fd, { bigint: true });
      if (opened.dev !== named.dev || opened.ino !== named.ino) {
        throw new Error("Application Support ancestor changed.");
      }
      rejectAclGrants(fd);
      // A profile under a repository is never an owner-selected profile.
      try { lstatSync(join(path, ".git")); throw new Error("Repository path cannot be a profile."); }
      catch (error) { if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) throw error; }
    } finally { closeSync(fd); }
    if (dirname(path) === path) break;
  }
}

function checkedPin(path: string, fd: number, directory: boolean): Pin {
  const status = fstatSync(fd, { bigint: true });
  const named = lstatSync(path, { bigint: true });
  if (status.dev !== named.dev || status.ino !== named.ino ||
      (directory ? !status.isDirectory() : !status.isFile()) ||
      named.isSymbolicLink() || status.uid !== uid() ||
      (status.mode & 0o777n) !== (directory ? 0o700n : 0o600n) ||
      (!directory && status.nlink !== 1n)) {
    throw new Error(`Unsafe or changed profile entry: ${path}`);
  }
  rejectAclGrants(fd);
  return { path, fd, directory, dev: status.dev, ino: status.ino };
}

function existingPin(parentFd: number, parentPath: string, name: string, directory: boolean): Pin {
  const path = join(parentPath, name);
  const fd = directory
    ? openExistingDirectoryAt(parentFd, name)
    : openFileAt(parentFd, name, constants.O_RDONLY | constants.O_NONBLOCK);
  try { return checkedPin(path, fd, directory); }
  catch (error) { closeSync(fd); throw error; }
}

function optionalFilePin(parentFd: number, parentPath: string, name: string): Pin | undefined {
  try { return existingPin(parentFd, parentPath, name, false); }
  catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") return undefined;
    throw error;
  }
}

function manifestObject(fd: number): unknown {
  const status = fstatSync(fd, { bigint: true });
  if (status.size > BigInt(MAX_MANIFEST_BYTES)) throw new Error("profile.json exceeds size limit.");
  const bytes = Buffer.alloc(Number(status.size));
  let offset = 0;
  while (offset < bytes.length) {
    const count = readSync(fd, bytes, offset, bytes.length - offset, offset);
    if (count <= 0) throw new Error("profile.json changed during read.");
    offset += count;
  }
  const source = bytes.toString("utf8");
  if (!Buffer.from(source, "utf8").equals(bytes)) throw new Error("profile.json is not UTF-8.");
  // Extract top-level JSON keys and reject duplicates, including escaped spellings.
  const keys = new Set<string>();
  let index = 0;
  const white = (): void => { while (/\s/u.test(source[index] ?? "")) index += 1; };
  const parseString = (): string => {
    const start = index;
    if (source[index] !== '"') throw new Error("Invalid profile.json key.");
    index += 1;
    while (index < source.length) {
      if (source[index] === "\\") { index += 2; continue; }
      if (source[index++] === '"') return JSON.parse(source.slice(start, index)) as string;
    }
    throw new Error("Invalid profile.json string.");
  };
  white();
  if (source[index++] !== "{") throw new Error("profile.json must be an object.");
  white();
  if (source[index] !== "}") {
    for (;;) {
      white();
      const key = parseString();
      if (keys.has(key)) throw new Error("profile.json contains duplicate fields.");
      keys.add(key);
      white();
      if (source[index++] !== ":") throw new Error("Invalid profile.json field.");
      white();
      const start = index;
      let depth = 0; let quoted = false; let escaped = false;
      for (; index < source.length; index += 1) {
        const char = source[index]!;
        if (quoted) { if (escaped) escaped = false; else if (char === "\\") escaped = true; else if (char === '"') quoted = false; continue; }
        if (char === '"') { quoted = true; continue; }
        if (char === "{" || char === "[") depth += 1;
        else if (char === "}" || char === "]") { if (depth === 0) break; depth -= 1; }
        else if (char === "," && depth === 0) break;
      }
      JSON.parse(source.slice(start, index));
      white();
      if (source[index] === "}") break;
      if (source[index++] !== ",") throw new Error("Invalid profile.json separator.");
    }
  }
  if (source[index++] !== "}") throw new Error("Invalid profile.json end.");
  white();
  if (index !== source.length) throw new Error("Invalid profile.json trailing bytes.");
  return JSON.parse(source) as unknown;
}

function checkManifest(fd: number, locator: BridgeProfileLocator, workspaceRoot: string): void {
  const data = manifestObject(fd);
  if (typeof data !== "object" || data === null || Array.isArray(data) ||
      (data as Record<string, unknown>).profile_id !== locator.profileId ||
      (data as Record<string, unknown>).runtime_root !== locator.runtimeRoot ||
      (data as Record<string, unknown>).workspace_root !== workspaceRoot) {
    throw new Error("profile.json does not bind the selected profile.");
  }
}

function validatePinned(active: ActiveCut): void {
  checkAncestors(active.locator.applicationSupportRoot);
  for (const pin of active.pins) {
    const fresh = openDirectoryOrFile(pin.path, pin.directory);
    try {
      const checked = checkedPin(pin.path, fresh, pin.directory);
      if (checked.dev !== pin.dev || checked.ino !== pin.ino) throw new Error("Profile entry identity changed.");
    } finally { closeSync(fresh); }
  }
  for (const path of active.absentFiles) {
    try { lstatSync(path); throw new Error("Profile file appeared after validation."); }
    catch (error) { if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) throw error; }
  }
  const manifest = active.pins.find((pin) => basename(pin.path) === "profile.json");
  if (manifest === undefined) throw new Error("Missing pinned profile.json.");
  checkManifest(manifest.fd, active.locator, active.context.workspaceRoot);
  if (process.env.FINANCE_RUNTIME_ROOT !== active.locator.runtimeRoot) {
    throw new Error("FINANCE_RUNTIME_ROOT changed during cut.");
  }
  const stage = checkedPin(active.stagePath, active.stageFd, true);
  if (stage.dev !== activeStageIdentity.get(active)?.dev || stage.ino !== activeStageIdentity.get(active)?.ino) {
    throw new Error("Bridge stage identity changed.");
  }
}

const activeStageIdentity = new WeakMap<ActiveCut, { dev: bigint; ino: bigint }>();

function openDirectoryOrFile(path: string, directory: boolean): number {
  if (directory) return openDirectory(path);
  const parentFd = openDirectory(dirname(path));
  try { return openFileAt(parentFd, basename(path), constants.O_RDONLY | constants.O_NONBLOCK); }
  finally { closeSync(parentFd); }
}

function sourceMatches(active: ActiveCut, source: BridgeSourceIdentity): void {
  if (!Number.isSafeInteger(source.fd) || source.fd < 0 ||
      typeof source.identity !== "object" || source.identity === null) {
    throw new Error("Invalid source descriptor identity.");
  }
  const now = descriptorIdentitySync(source.fd);
  const expected = source.identity;
  const handoffDirectory = now.isDirectory && !now.isFile;
  if ((!now.isFile && !handoffDirectory) || now.uid !== Number(uid()) ||
      (now.mode & 0o777) !== (handoffDirectory ? 0o700 : 0o600) ||
      now.isDirectory !== expected.isDirectory || now.isFile !== expected.isFile ||
      now.dev !== expected.dev || now.ino !== expected.ino ||
      now.uid !== expected.uid || now.mode !== expected.mode || now.size !== expected.size ||
      now.ctimeNs !== expected.ctimeNs || now.mtimeNs !== expected.mtimeNs) {
    throw new Error("Bridge cut source descriptor changed or is unsafe.");
  }
  if (handoffDirectory) {
    const checked = checkedPin(active.context.handoffRoot, source.fd, true);
    if (checked.dev !== now.dev || checked.ino !== now.ino) {
      throw new Error("Bridge handoff directory identity changed.");
    }
  } else if (fstatSync(source.fd, { bigint: true }).nlink !== 1n) {
    throw new Error("Bridge cut source file is hard-linked.");
  }
  rejectAclGrants(source.fd);
}

/** Check the live, internally acquired cut and profile pins before/after source I/O. */
export function assertBridgeCut(
  context: BridgeCutContext, sink: PrivateBridgeStageSink, source?: BridgeSourceIdentity,
): void {
  const active = typeof context === "object" && context !== null ? activeContexts.get(context) : undefined;
  if (active === undefined || activeSinks.get(sink) !== active || !active.active) {
    throw new Error("Bridge cut and stage are not a live matched owner session.");
  }
  active.lease.assertValid();
  validatePinned(active);
  if (source !== undefined) sourceMatches(active, source);
  active.lease.assertValid();
}

function safeName(name: string): void {
  if (typeof name !== "string" || name.length === 0 || Buffer.byteLength(name, "utf8") > 255 ||
      name === "." || name === ".." || name.includes("/") || name.includes("\\") || name.includes("\0")) {
    throw new Error("Stage output needs one safe basename.");
  }
}

function verifyStageOutputs(active: ActiveCut): void {
  active.lease.assertValid();
  const expected = new Set(active.verifiedOutputs.map((output) => output.relativeName));
  const actual = listAt(active.stageFd);
  if (actual.length !== expected.size || actual.some((name) => !expected.has(name))) {
    throw new Error("Bridge stage inventory changed before finalization.");
  }
  const block = Buffer.allocUnsafe(64 * 1024);
  for (const output of active.verifiedOutputs) {
    active.lease.assertValid();
    const fd = openFileAt(active.stageFd, output.relativeName, constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      const named = checkedPin(join(active.stagePath, output.relativeName), fd, false);
      const pinned = fstatSync(output.fd, { bigint: true });
      const before = fstatSync(fd, { bigint: true });
      if (named.dev !== output.dev || named.ino !== output.ino ||
          pinned.dev !== output.dev || pinned.ino !== output.ino ||
          before.size !== BigInt(output.byteSize) || pinned.size !== before.size) {
        throw new Error("Bridge stage output identity or size changed before finalization.");
      }
      const hash = createHash("sha256");
      for (let offset = 0; offset < output.byteSize;) {
        active.lease.assertValid();
        const count = readSync(fd, block, 0, Math.min(block.length, output.byteSize - offset), offset);
        if (count <= 0) throw new Error("Bridge stage output changed during finalization.");
        hash.update(block.subarray(0, count));
        offset += count;
      }
      const after = fstatSync(fd, { bigint: true });
      if (after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size ||
          after.ctimeNs !== before.ctimeNs || after.mtimeNs !== before.mtimeNs ||
          hash.digest("hex") !== output.sha256) {
        throw new Error("Bridge stage output changed before finalization.");
      }
    } finally { closeSync(fd); }
  }
  active.lease.assertValid();
  const finalNames = listAt(active.stageFd);
  if (finalNames.length !== expected.size || finalNames.some((name) => !expected.has(name))) {
    throw new Error("Bridge stage inventory changed during finalization.");
  }
}

function openProfile(locator: BridgeProfileLocator): { pins: Pin[]; absentFiles: string[]; workspaceRoot: string; handoffRoot: string; workFd: number; profileRoot: string } {
  if (typeof locator !== "object" || locator === null || !PROFILE_ID.test(locator.profileId)) {
    throw new Error("Invalid profile locator.");
  }
  checkAncestors(locator.applicationSupportRoot);
  const profileRoot = join(locator.applicationSupportRoot, "Finance-Codex", "profiles", locator.profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  if (locator.runtimeRoot !== runtimeRoot || process.env.FINANCE_RUNTIME_ROOT !== runtimeRoot) {
    throw new Error("FINANCE_RUNTIME_ROOT does not match the selected profile.");
  }
  const pins: Pin[] = [];
  const absentFiles: string[] = [];
  try {
    const rootFd = openDirectory(locator.applicationSupportRoot);
    let parentFd = rootFd;
    try {
      for (const name of ["Finance-Codex", "profiles", locator.profileId]) {
        const parentPath = pins.at(-1)?.path ?? locator.applicationSupportRoot;
        const pin = existingPin(parentFd, parentPath, name, true);
        pins.push(pin); parentFd = pin.fd;
      }
      const profileFd = parentFd;
      for (const name of ["runtime", "workspace", "backups", "work", "restore"]) {
        pins.push(existingPin(profileFd, profileRoot, name, true));
      }
      const runtimeFd = pins.find((p) => p.path === runtimeRoot)!.fd;
      const workspaceFd = pins.find((p) => p.path === workspaceRoot)!.fd;
      const runtimeDb = existingPin(runtimeFd, runtimeRoot, "database", true); pins.push(runtimeDb);
      const workspaceDb = existingPin(workspaceFd, workspaceRoot, "database", true); pins.push(workspaceDb);
      pins.push(existingPin(workspaceFd, workspaceRoot, "handoff", true));
      const manifest = existingPin(profileFd, profileRoot, "profile.json", false); pins.push(manifest);
      for (const [fd, path, name] of [
        [runtimeDb.fd, runtimeDb.path, "finance.db"],
        [workspaceDb.fd, workspaceDb.path, "staging.sqlite"],
      ] as const) {
        const optional = optionalFilePin(fd, path, name);
        if (optional !== undefined) pins.push(optional);
        else absentFiles.push(join(path, name));
      }
      checkManifest(manifest.fd, locator, workspaceRoot);
      return { pins, absentFiles, workspaceRoot, handoffRoot: join(workspaceRoot, "handoff"),
        workFd: pins.find((p) => p.path === join(profileRoot, "work"))!.fd, profileRoot };
    } finally { closeSync(rootFd); }
  } catch (error) {
    for (const pin of pins.reverse()) closeSync(pin.fd);
    throw error;
  }
}

/** Owns the exclusive lock; neither an FD nor a caller flag can supply its authority. */
export async function withExclusiveBridgeCut<T>(
  locator: BridgeProfileLocator,
  callback: (context: BridgeCutContext, sink: PrivateBridgeStageSink) => Promise<T>,
  options: BridgeCutOptions = {},
): Promise<T> {
  const profile = openProfile(locator);
  let gate: ProfileGate | undefined;
  let lease: ExclusiveProfileGateLease | undefined;
  let stageFd: number | undefined;
  let active: ActiveCut | undefined;
  let result: T | undefined;
  let completed = false;
  let failed = false;
  let primaryError: unknown;
  try {
    gate = openProfileGate(profile.profileRoot);
    lease = await gate.acquireExclusive(options.waitMs ?? 5_000, options.maxHoldMs ?? 30_000);
    lease.assertValid();
    const cutId = randomBytes(16).toString("hex");
    const stageRelativeName = `${STAGE_PREFIX}${cutId}`;
    stageFd = createDirectoryExclusiveAt(profile.workFd, stageRelativeName);
    const stagePath = join(profile.profileRoot, "work", stageRelativeName);
    const stage = checkedPin(stagePath, stageFd, true);
    fsyncSync(profile.workFd);
    const context: BridgeCutContext = Object.freeze({
      profileId: locator.profileId, cutId, workspaceRoot: profile.workspaceRoot,
      handoffRoot: profile.handoffRoot, stageRelativeName, stagePath,
    });
    const outputFds: number[] = [];
    const verifiedOutputs: VerifiedOutput[] = [];
    let stagedBytes = 0;
    const sink: PrivateBridgeStageSink = Object.freeze({
      stagePath,
      async writeValidated(relativeName: string, bytes: Buffer): Promise<BridgeStageWrite> {
        assertBridgeCut(context, sink);
        safeName(relativeName);
        if (!Buffer.isBuffer(bytes) || bytes.length > MAX_STAGE_FILE_BYTES ||
            stagedBytes + bytes.length > MAX_STAGE_TOTAL_BYTES) {
          throw new Error("Stage bytes must be a bounded Buffer.");
        }
        stagedBytes += bytes.length;
        const fd = openFileAt(stageFd!, relativeName, constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | constants.O_NONBLOCK, 0o600);
        outputFds.push(fd);
        const before = checkedPin(join(stagePath, relativeName), fd, false);
        try {
          for (let offset = 0; offset < bytes.length;) {
            assertBridgeCut(context, sink);
            const count = writeSync(fd, bytes, offset, bytes.length - offset);
            if (count <= 0) throw new Error("Stage output write made no progress.");
            offset += count;
          }
          fsyncSync(fd);
          const after = checkedPin(join(stagePath, relativeName), fd, false);
          const writtenStatus = fstatSync(fd, { bigint: true });
          if (after.dev !== before.dev || after.ino !== before.ino ||
              writtenStatus.size !== BigInt(bytes.length)) {
            throw new Error("Stage output changed during write.");
          }
          const verifiedHash = createHash("sha256");
          const block = Buffer.allocUnsafe(64 * 1024);
          for (let offset = 0; offset < bytes.length;) {
            active!.lease.assertValid();
            const count = readSync(fd, block, 0, Math.min(block.length, bytes.length - offset), offset);
            if (count <= 0 || !block.subarray(0, count).equals(bytes.subarray(offset, offset + count))) {
              throw new Error("Stage output differs from supplied bytes.");
            }
            verifiedHash.update(block.subarray(0, count));
            offset += count;
          }
          const verifiedStatus = fstatSync(fd, { bigint: true });
          if (verifiedStatus.size !== writtenStatus.size ||
              verifiedStatus.ctimeNs !== writtenStatus.ctimeNs ||
              verifiedStatus.mtimeNs !== writtenStatus.mtimeNs ||
              verifiedStatus.dev !== writtenStatus.dev || verifiedStatus.ino !== writtenStatus.ino) {
            throw new Error("Stage output changed during verification.");
          }
          assertBridgeCut(context, sink);
          fsyncSync(stageFd!);
          const entry = Object.freeze({ relativeName, byteSize: bytes.length,
            sha256: verifiedHash.digest("hex") });
          verifiedOutputs.push({ relativeName, fd, dev: after.dev, ino: after.ino,
            byteSize: entry.byteSize, sha256: entry.sha256 });
          return entry;
        } catch (error) { throw error; }
      },
    });
    active = { context, sink, pins: profile.pins, absentFiles: profile.absentFiles, stageFd, stagePath, gate, lease,
      outputFds, verifiedOutputs, locator, active: true };
    activeContexts.set(context, active); activeSinks.set(sink, active);
    activeStageIdentity.set(active, { dev: stage.dev, ino: stage.ino });
    assertBridgeCut(context, sink);
    result = await callback(context, sink);
    assertBridgeCut(context, sink);
    for (const fd of outputFds) fsyncSync(fd);
    fsyncSync(stageFd);
    fsyncSync(profile.workFd);
    assertBridgeCut(context, sink);
    verifyStageOutputs(active);
    completed = true;
  } catch (error) {
    failed = true;
    primaryError = error;
  } finally {
    const cleanupErrors: unknown[] = [];
    const attempt = (action: () => void): void => {
      try { action(); } catch (error) { cleanupErrors.push(error); }
    };
    if (active !== undefined) {
      active.active = false;
      activeContexts.delete(active.context); activeSinks.delete(active.sink);
      for (const fd of active.outputFds.reverse()) attempt(() => closeSync(fd));
    }
    if (stageFd !== undefined) { const fd = stageFd; attempt(() => closeSync(fd)); }
    if (lease !== undefined) { const held = lease; attempt(() => held.close()); }
    if (gate !== undefined) { const opened = gate; attempt(() => opened.close()); }
    for (const pin of profile.pins.reverse()) attempt(() => closeSync(pin.fd));
    if (cleanupErrors.length > 0) {
      throw new AggregateError(failed ? [primaryError, ...cleanupErrors] : cleanupErrors,
        "Bridge cut cleanup failed; export result is not accepted.");
    }
  }
  if (failed) throw primaryError;
  if (!completed) throw new Error("Bridge cut ended without a completed export callback.");
  return result as T;
}
