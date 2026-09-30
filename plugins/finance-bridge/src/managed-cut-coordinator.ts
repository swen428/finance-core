/** Fixed synthetic Core component cut. No Host/Bridge export or publication. */
import { spawn, type ChildProcess } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { closeSync, constants, fstatSync, fsyncSync, lstatSync, readFileSync, realpathSync } from "node:fs";
import { basename, isAbsolute, join, resolve } from "node:path";
import type { Duplex } from "node:stream";

import { revalidatePythonExecutableForSpawn, type FinanceBridgeConfig } from "./config.js";
import { verifyCoreDistributionV1 } from "./core-distribution-v1.js";
import { openProfileGate, type ExclusiveProfileGateLease } from "./profile-gate.js";
import {
  createDirectoryExclusiveAt, openDirectory, openExistingDirectoryAt,
  openFileAt, rejectAclGrants,
} from "./posix.js";

const VERSION = "delegated-cut-worker-v1";
const MAX_FRAME = 8192;
const MAX_HOLD_MS = 30_000;
const GATE_CANCEL_POLL_MS = 100;
const REAP_GRACE_MS = 1_000;
const HASH = /^[0-9a-f]{64}$/u;
const HEX32 = /^[0-9a-f]{32}$/u;
const DECIMAL = /^(?:0|[1-9][0-9]*)$/u;
const PROFILE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/u;
const BOOTSTRAP = [
  "import importlib.util,pathlib,runpy,sys",
  "root=pathlib.Path(sys.argv[1]).resolve(strict=True)",
  "package=(root/'finance_core').resolve(strict=True)",
  "sys.path.insert(0,str(root))",
  "top=importlib.util.find_spec('finance_core')",
  "name='finance_core.'+sys.argv[2]",
  "module=importlib.util.find_spec(name)",
  "origins=[pathlib.Path(spec.origin).resolve(strict=True) for spec in (top,module) if spec is not None and spec.origin is not None]",
  "assert len(origins)==2 and all(package==path.parent or package in path.parents for path in origins)",
  "sys.argv=[name]",
  "runpy.run_module(name,run_name='__main__',alter_sys=True)",
].join(";");

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

type Plain = Record<string, unknown>;
type ChildOperation = "core_snapshot" | "core_readback";
type CutContext = {
  readonly gate: ReturnType<typeof openProfileGate>;
  readonly lease: ExclusiveProfileGateLease;
  readonly profileFd: number;
  readonly workFd: number;
  readonly stageFd: number;
  readonly stagePath: string;
  released: boolean;
  uncertain: boolean;
};

// An unresolved close cannot be converted into lease release by a timer.
const uncertainCuts = new Set<CutContext>();
let unhealthy = false;

function releaseContext(context: CutContext): void {
  if (context.released) return;
  // Child close/reap (or a pre-spawn failure) is the only caller path.
  closeSync(context.stageFd);
  context.lease.close();
  context.gate.close();
  closeSync(context.workFd);
  closeSync(context.profileFd);
  context.released = true;
}

function exactObject(value: unknown, keys: readonly string[]): Plain {
  if (typeof value !== "object" || value === null || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) {
    throw new Error("Managed cut protocol frame is invalid.");
  }
  return value as Plain;
}

function boundedMs(value: number | undefined, fallback: number): number {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result <= 0 || result > MAX_HOLD_MS) {
    throw new Error("Managed cut timeout must be an integer from 1 through 30000 milliseconds.");
  }
  return result;
}

function assertNotCancelled(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new Error("Managed cut cancelled.");
}

async function acquireCutLease(gate: ReturnType<typeof openProfileGate>, waitMs: number,
  maxHoldMs: number, signal: AbortSignal | undefined): Promise<ExclusiveProfileGateLease> {
  assertNotCancelled(signal);
  if (signal === undefined) return await gate.acquireExclusive(waitMs, maxHoldMs);
  const deadline = performance.now() + waitMs;
  for (;;) {
    assertNotCancelled(signal);
    const remaining = Math.ceil(deadline - performance.now());
    if (remaining <= 0) throw new Error("Profile gate wait deadline exceeded.");
    let lease: ExclusiveProfileGateLease;
    try {
      lease = await gate.acquireExclusive(Math.min(remaining, GATE_CANCEL_POLL_MS), maxHoldMs);
    } catch (error) {
      if (!(error instanceof Error) || error.message !== "Profile gate wait deadline exceeded.") {
        throw error;
      }
      continue;
    }
    if (signal.aborted || performance.now() > deadline) {
      lease.close();
      assertNotCancelled(signal);
      throw new Error("Profile gate wait deadline exceeded.");
    }
    return lease;
  }
}

function normalizedLimits(input: ManagedCoreSnapshotLimits): Record<string, number> {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new Error("Managed cut limits are required.");
  }
  const values = {
    max_core_db_bytes: input.maxCoreDbBytes,
    max_stage_bytes: input.maxStageBytes,
    min_free_bytes: input.minFreeBytes,
    backup_pages_per_step: input.backupPagesPerStep,
  };
  if (Object.values(values).some((value) => !Number.isSafeInteger(value) || value <= 0) ||
      values.max_stage_bytes < values.max_core_db_bytes || values.backup_pages_per_step > 1024) {
    throw new Error("Managed cut limits are invalid.");
  }
  return values;
}

function checkedDirectory(fd: number, path: string): void {
  const opened = fstatSync(fd, { bigint: true });
  const named = lstatSync(path, { bigint: true });
  if (!opened.isDirectory() || named.isSymbolicLink() ||
      opened.dev !== named.dev || opened.ino !== named.ino ||
      opened.uid !== BigInt(process.getuid!()) || (opened.mode & 0o777n) !== 0o700n) {
    throw new Error("Managed cut directory is unsafe or changed.");
  }
  rejectAclGrants(fd);
}

function checkedRegistration(fd: number, path: string, profileId: string,
  profileRoot: string): { digest: string; schema: string } {
  const opened = fstatSync(fd, { bigint: true });
  const named = lstatSync(path, { bigint: true });
  if (!opened.isFile() || named.isSymbolicLink() || opened.dev !== named.dev ||
      opened.ino !== named.ino || opened.uid !== BigInt(process.getuid!()) ||
      (opened.mode & 0o777n) !== 0o600n || opened.nlink !== 1n || opened.size <= 0n ||
      opened.size > 65_536n) {
    throw new Error("Managed cut registration is unsafe.");
  }
  rejectAclGrants(fd);
  const bytes = readFileSync(fd);
  const parsed = JSON.parse(bytes.toString("utf8")) as unknown;
  const entry = exactObject(parsed, ["version", "profile_id", "runtime_root", "workspace_root",
    "staging_database", "main_device", "main_inode", "migration_contract_sha256", "instance_id"]);
  if (entry.version !== 1 || entry.profile_id !== profileId ||
      entry.runtime_root !== join(profileRoot, "runtime") ||
      entry.workspace_root !== join(profileRoot, "workspace") ||
      entry.staging_database !== join(profileRoot, "workspace", "database", "staging.sqlite") ||
      typeof entry.instance_id !== "string" || !HEX32.test(entry.instance_id) ||
      typeof entry.migration_contract_sha256 !== "string" ||
      !HASH.test(entry.migration_contract_sha256)) {
    throw new Error("Managed cut registration does not match profile.");
  }
  const after = fstatSync(fd, { bigint: true });
  if (after.dev !== opened.dev || after.ino !== opened.ino || after.size !== opened.size ||
      after.ctimeNs !== opened.ctimeNs || after.mtimeNs !== opened.mtimeNs) {
    throw new Error("Managed cut registration changed during inspection.");
  }
  return { digest: createHash("sha256").update(bytes).digest("hex"),
    schema: entry.migration_contract_sha256 };
}

function decimalIdentity(value: unknown): bigint {
  if (typeof value !== "string" || !DECIMAL.test(value)) {
    throw new Error("Managed cut identity must be a canonical decimal string.");
  }
  const parsed = BigInt(value);
  if (parsed > 0xffff_ffff_ffff_ffffn) throw new Error("Managed cut identity exceeds native range.");
  return parsed;
}

function sendFrame(control: Duplex, value: Plain): void {
  const frame = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  if (frame.length > MAX_FRAME || control.destroyed) throw new Error("Managed cut control is closed.");
  control.write(frame);
}

function commonReceipt(frame: Plain, operation: ChildOperation, cutId: string,
  workerId: string, profileId: string, artifact: string, schema: string): void {
  if (frame.version !== VERSION || frame.cut_id !== cutId || frame.worker_id !== workerId ||
      frame.profile_id !== profileId || frame.operation !== operation ||
      frame.artifact_sha256 !== artifact || frame.schema_sha256 !== schema ||
      frame.output_role !== "core.sqlite") {
    throw new Error("Managed cut receipt binding is invalid.");
  }
}

function stagedReceipt(frame: unknown, cutId: string, workerId: string, profileId: string,
  artifact: string, schema: string): Plain {
  const staged = exactObject(frame, ["version", "type", "cut_id", "worker_id", "profile_id",
    "operation", "artifact_sha256", "schema_sha256", "output_role", "byte_length", "sha256",
    "page_count", "stage_dev", "stage_ino", "output_dev", "output_ino", "backup_complete", "source_closed"]);
  commonReceipt(staged, "core_snapshot", cutId, workerId, profileId, artifact, schema);
  if (staged.type !== "staged" || staged.backup_complete !== true || staged.source_closed !== true ||
      !Number.isSafeInteger(staged.byte_length) || (staged.byte_length as number) <= 0 ||
      !Number.isSafeInteger(staged.page_count) || (staged.page_count as number) <= 0 ||
      typeof staged.sha256 !== "string" || !HASH.test(staged.sha256)) {
    throw new Error("Managed cut staged receipt is invalid.");
  }
  for (const key of ["stage_dev", "stage_ino", "output_dev", "output_ino"]) decimalIdentity(staged[key]);
  return staged;
}

function verifiedReceipt(frame: unknown, cutId: string, workerId: string, profileId: string,
  artifact: string, schema: string, staged: Plain): Plain {
  const verified = exactObject(frame, ["version", "type", "cut_id", "worker_id", "profile_id",
    "operation", "artifact_sha256", "schema_sha256", "output_role", "byte_length", "sha256",
    "page_count", "schema_object_count", "journal_mode", "reader_closed"]);
  commonReceipt(verified, "core_readback", cutId, workerId, profileId, artifact, schema);
  if (verified.type !== "verified" || verified.reader_closed !== true ||
      verified.journal_mode !== "delete" || verified.byte_length !== staged.byte_length ||
      verified.sha256 !== staged.sha256 || verified.page_count !== staged.page_count ||
      !Number.isSafeInteger(verified.schema_object_count) || (verified.schema_object_count as number) < 0) {
    throw new Error("Managed cut readback receipt is invalid.");
  }
  return verified;
}

async function runFixedChild(context: CutContext, options: ManagedCoreSnapshotOptions,
  operation: ChildOperation, request: Plain, deadline: number): Promise<Plain> {
  context.lease.assertValid();
  if (performance.now() >= deadline || options.signal?.aborted) {
    throw new Error("Managed cut deadline expired or was cancelled.");
  }
  await revalidatePythonExecutableForSpawn(options.config);
  context.lease.assertValid();
  if (performance.now() >= deadline || options.signal?.aborted) {
    throw new Error("Managed cut deadline expired or was cancelled.");
  }
  context.lease.reserveChild();
  const module = operation === "core_snapshot" ? "managed_cut_worker" : "managed_snapshot_reader";
  let child: ChildProcess;
  try {
    child = spawn(options.config.pythonExecutable,
      ["-I", "-B", "-c", BOOTSTRAP, options.config.coreDistributionRoot, module], {
        cwd: options.config.coreDistributionRoot, shell: false, detached: false,
        env: {
          FINANCE_RUNTIME_ROOT: join(options.applicationSupportRoot, "Finance-Codex", "profiles", options.profileId, "runtime"),
          FINANCE_CUT_APPLICATION_SUPPORT: options.applicationSupportRoot,
          FINANCE_CUT_PROFILE_ID: options.profileId,
          FINANCE_CUT_STAGE_PATH: context.stagePath,
          FINANCE_CUT_ARTIFACT_SHA256: options.config.agentProfileV2.coreManifestSha256,
          LANG: "C.UTF-8", LC_ALL: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1",
          PYTHONNOUSERSITE: "1", PYTHONUTF8: "1",
        },
        stdio: ["ignore", "ignore", "ignore", "pipe", context.lease.fdForChild(),
          context.profileFd, context.stageFd],
      });
  } catch (error) {
    context.lease.unbindChild();
    throw error;
  }
  const control = child.stdio[3] as Duplex | null;
  return await new Promise<Plain>((resolvePromise, reject) => {
    let bytes = Buffer.alloc(0);
    let ready = false;
    let terminal: Plain | undefined;
    let failure: Error | undefined;
    let settled = false;
    let termTimer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;
    const cleanup = (): void => {
      clearTimeout(deadlineTimer);
      if (termTimer !== undefined) clearTimeout(termTimer);
      if (killTimer !== undefined) clearTimeout(killTimer);
      options.signal?.removeEventListener("abort", onAbort);
    };
    const terminate = (reason: Error): void => {
      if (failure !== undefined || settled) return;
      failure = reason;
      child.kill("SIGTERM");
      termTimer = setTimeout(() => {
        if (settled) return;
        child.kill("SIGKILL");
        killTimer = setTimeout(() => {
          if (settled) return;
          settled = true;
          unhealthy = true;
          context.uncertain = true;
          uncertainCuts.add(context);
          cleanup();
          reject(new Error("Managed cut child close/reap is unknown; profile remains held."));
        }, REAP_GRACE_MS);
      }, REAP_GRACE_MS);
    };
    const onAbort = (): void => terminate(new Error("Managed cut cancelled."));
    const deadlineTimer = setTimeout(() => terminate(new Error("Managed cut deadline expired.")),
      Math.max(0, Math.floor(deadline - performance.now())));
    child.once("error", () => terminate(new Error("Managed cut child startup failed.")));
    control?.on("error", () => terminate(new Error("Managed cut control failed.")));
    control?.on("data", (chunk: Buffer | string) => {
      if (failure !== undefined || settled) return;
      bytes = Buffer.concat([bytes, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
      if (bytes.length > MAX_FRAME * 2) return terminate(new Error("Managed cut frame exceeded limit."));
      for (;;) {
        const end = bytes.indexOf(10);
        if (end < 0) break;
        const line = bytes.subarray(0, end);
        bytes = bytes.subarray(end + 1);
        if (line.length > MAX_FRAME) return terminate(new Error("Managed cut frame exceeded limit."));
        let parsed: unknown;
        try { parsed = JSON.parse(line.toString("utf8")); }
        catch { return terminate(new Error("Managed cut frame is malformed.")); }
        if (!ready) {
          try {
            const response = exactObject(parsed, ["version", "type", "cut_id", "worker_id"]);
            if (response.version !== VERSION || response.type !== "ready" ||
                response.cut_id !== request.cut_id || response.worker_id !== request.worker_id) {
              throw new Error("Managed cut handshake differs.");
            }
            ready = true;
            sendFrame(control!, { version: VERSION, type: "go", cut_id: request.cut_id,
              worker_id: request.worker_id });
          } catch { return terminate(new Error("Managed cut handshake is invalid.")); }
        } else if (terminal === undefined) {
          try { terminal = exactObject(parsed, Object.keys(parsed as Plain)); }
          catch { return terminate(new Error("Managed cut terminal frame is invalid.")); }
        } else {
          return terminate(new Error("Managed cut emitted extra frames."));
        }
      }
    });
    child.once("close", (code, signal) => {
      // A success frame and an `exit` event are insufficient; only close has
      // observed process exit and all inherited streams closed.
      let releaseError: Error | undefined;
      try { context.lease.unbindChild(); }
      catch {
        unhealthy = true;
        context.uncertain = true;
        uncertainCuts.add(context);
        releaseError = new Error("Managed cut child reservation could not be released.");
      }
      if (settled) {
        // The bounded wait reported unknown, but an eventual actual close is
        // now observed. Keep the attempt failed and release exclusion safely.
        if (releaseError === undefined && context.uncertain) {
          try {
            releaseContext(context);
            uncertainCuts.delete(context);
            unhealthy = uncertainCuts.size !== 0;
          } catch {
            unhealthy = true;
          }
        }
        return;
      }
      settled = true;
      cleanup();
      if (releaseError !== undefined) return reject(releaseError);
      if (failure !== undefined) return reject(failure);
      if (signal !== null || code !== 0 || !ready || terminal === undefined || bytes.length !== 0) {
        return reject(new Error("Managed cut child ended without a valid terminal receipt."));
      }
      resolvePromise(terminal);
    });
    options.signal?.addEventListener("abort", onAbort, { once: true });
    // Abort can occur after the pre-spawn check but before this listener exists.
    if (options.signal?.aborted) onAbort();
    if (failure === undefined && control === null) terminate(new Error("Managed cut control pipe is missing."));
    else if (failure === undefined && control !== null) {
      try { sendFrame(control, request); }
      catch { terminate(new Error("Managed cut request could not be sent.")); }
    }
  });
}

/** Component-only: one EX covers both fixed children and their actual reap. */
export async function runManagedCoreSnapshot(options: ManagedCoreSnapshotOptions): Promise<ManagedCoreSnapshotReceipt> {
  if (unhealthy) throw new Error("Managed cut coordinator is unhealthy after uncertain child closure.");
  if (!PROFILE_ID.test(options.profileId) || !isAbsolute(options.applicationSupportRoot) ||
      resolve(options.applicationSupportRoot) !== options.applicationSupportRoot ||
      realpathSync(options.applicationSupportRoot) !== options.applicationSupportRoot ||
      basename(options.applicationSupportRoot) !== "Application Support") {
    throw new Error("Managed cut requires a canonical synthetic profile locator.");
  }
  const limits = normalizedLimits(options.limits);
  const waitMs = boundedMs(options.waitMs, 5_000);
  const maxHoldMs = boundedMs(options.maxHoldMs, 30_000);
  assertNotCancelled(options.signal);
  await revalidatePythonExecutableForSpawn(options.config);
  assertNotCancelled(options.signal);
  await verifyCoreDistributionV1(options.config);
  assertNotCancelled(options.signal);
  const profileRoot = join(options.applicationSupportRoot, "Finance-Codex", "profiles", options.profileId);
  const profileFd = openDirectory(profileRoot);
  let workFd: number | undefined;
  let stageFd: number | undefined;
  let gate: ReturnType<typeof openProfileGate> | undefined;
  let lease: ExclusiveProfileGateLease | undefined;
  let context: CutContext | undefined;
  try {
    checkedDirectory(profileFd, profileRoot);
    workFd = openExistingDirectoryAt(profileFd, "work");
    checkedDirectory(workFd, join(profileRoot, "work"));
    const registrationPath = join(profileRoot, ".managed-staging.v1.json");
    const registrationFd = openFileAt(profileFd, ".managed-staging.v1.json", constants.O_RDONLY | constants.O_NONBLOCK);
    let registration: { digest: string; schema: string };
    try { registration = checkedRegistration(registrationFd, registrationPath, options.profileId,
      profileRoot); }
    finally { closeSync(registrationFd); }
    gate = openProfileGate(profileRoot);
    lease = await acquireCutLease(gate, waitMs, maxHoldMs, options.signal);
    lease.assertValid();
    assertNotCancelled(options.signal);
    const cutId = randomBytes(16).toString("hex");
    const stageName = `core-cut-${cutId}`;
    stageFd = createDirectoryExclusiveAt(workFd, stageName);
    const stagePath = join(profileRoot, "work", stageName);
    checkedDirectory(stageFd, stagePath);
    fsyncSync(workFd);
    context = { gate, lease, profileFd, workFd, stageFd, stagePath,
      released: false, uncertain: false };
    const deadline = performance.now() + maxHoldMs;
    const common = {
      version: VERSION, cut_id: cutId, profile_id: options.profileId,
      registration_sha256: registration.digest,
      artifact_sha256: options.config.agentProfileV2.coreManifestSha256,
      schema_sha256: registration.schema,
      limits_sha256: createHash("sha256").update(JSON.stringify(Object.fromEntries(
        Object.entries(limits).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
      ))).digest("hex"),
      limits,
    };
    const workerId = randomBytes(16).toString("hex");
    const stagedFrame = await runFixedChild(context, options, "core_snapshot", {
      ...common, worker_id: workerId, operation: "core_snapshot",
      remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
    }, deadline);
    const staged = stagedReceipt(stagedFrame, cutId, workerId, options.profileId,
      common.artifact_sha256, common.schema_sha256);
    const stageStat = fstatSync(stageFd, { bigint: true });
    if (decimalIdentity(staged.stage_dev) !== stageStat.dev ||
        decimalIdentity(staged.stage_ino) !== stageStat.ino) {
      throw new Error("Managed cut stage identity changed.");
    }
    lease.assertValid();
    const readerId = randomBytes(16).toString("hex");
    const readerFrame = await runFixedChild(context, options, "core_readback", {
      ...common, worker_id: readerId, operation: "core_readback",
      remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
      staged: {
        byte_length: staged.byte_length, sha256: staged.sha256, page_count: staged.page_count,
        stage_dev: staged.stage_dev, stage_ino: staged.stage_ino,
        output_dev: staged.output_dev, output_ino: staged.output_ino,
      },
    }, deadline);
    const verified = verifiedReceipt(readerFrame, cutId, readerId, options.profileId,
      common.artifact_sha256, common.schema_sha256, staged);
    lease.assertValid();
    return Object.freeze({ cutId, stagePath, byteLength: verified.byte_length as number,
      sha256: verified.sha256 as string, pageCount: verified.page_count as number,
      schemaObjectCount: verified.schema_object_count as number, journalMode: "delete" });
  } finally {
    if (context !== undefined) {
      if (!context.uncertain) releaseContext(context);
    } else {
      if (stageFd !== undefined) closeSync(stageFd);
      if (lease !== undefined) lease.close();
      if (gate !== undefined) gate.close();
      if (workFd !== undefined) closeSync(workFd);
      closeSync(profileFd);
    }
  }
}
