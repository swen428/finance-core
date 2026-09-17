import { spawn as nodeSpawn, type SpawnOptions } from "node:child_process";
import type { Readable, Writable } from "node:stream";

import {
  revalidatePythonExecutableForSpawn,
  type FinanceBridgeConfig,
} from "./config.js";
import { verifyCoreDistributionV1 } from "./core-distribution-v1.js";
import {
  expectedBridgeOperationId,
  MAX_RESPONSE_BYTES,
  parseBridgeResponse,
  safeModelEvaluationRefusalDetailsV1,
  serializeBridgeRequest,
  type BridgeRequest,
  type BridgeResponse,
} from "./protocol.js";

const DEFAULT_REAP_GRACE_MS = 1_000;
const CORE_CLI_BOOTSTRAP = [
  "import importlib.util,pathlib,runpy,sys",
  "root=pathlib.Path(sys.argv[1]).resolve(strict=True)",
  "package=(root/'finance_core').resolve(strict=True)",
  "sys.path.insert(0,str(root))",
  "top=importlib.util.find_spec('finance_core')",
  "module=importlib.util.find_spec('finance_core.openclaw_staging_bridge.cli')",
  "origins=[pathlib.Path(spec.origin).resolve(strict=True) for spec in (top,module) if spec is not None and spec.origin is not None]",
  "assert len(origins)==2 and all(package==path.parent or package in path.parents for path in origins)",
  "sys.argv=['finance_core.openclaw_staging_bridge.cli']",
  "runpy.run_module('finance_core.openclaw_staging_bridge.cli',run_name='__main__',alter_sys=True)",
].join(";");

const BRIDGE_ERROR_CODES_BY_EXIT = new Map<number, ReadonlySet<string>>([
  [2, new Set(["WORKSPACE_MISSING"])],
  [3, new Set([
    "ENVELOPE_TOO_DEEP",
    "MALFORMED_ENVELOPE",
    "MISSING_IDEMPOTENCY_KEY",
    "OVERSIZED_ENVELOPE",
  ])],
  [4, new Set(["UNKNOWN_COMMAND"])],
  [5, new Set([
    "AI_FALLBACK_ARGUMENTS_REFUSED",
    "AI_FALLBACK_INTERNAL",
    "AI_FALLBACK_VALIDATION_REFUSED",
    "AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED",
    "AI_MODEL_CONFIG_REFUSED",
    "AI_MODEL_EVAL_REFUSED",
    "ARGUMENTS_REFUSED",
    "HANDOFF_NOT_FOUND",
    "HANDOFF_REFUSED",
    "INTAKE_NOT_FOUND",
    "NO_MATERIAL_CHANGE",
    "OCR_ENGINE_UNAVAILABLE",
    "PROPOSAL_NOT_FOUND",
    "TELEGRAM_SOURCE_REFUSED",
    "UNSUPPORTED_EDIT",
    "UNSUPPORTED_UPDATE_TYPE",
    "WORKSPACE_REFUSED",
  ])],
  [6, new Set([
    "ACTOR_MISMATCH",
    "AI_FALLBACK_CONFLICT",
    "AI_FALLBACK_NOT_ELIGIBLE",
    "AI_FALLBACK_NOT_FOUND",
    "AI_FALLBACK_POLICY_REFUSED",
    "AI_MODEL_COMPATIBILITY_CONFLICT",
    "AI_MODEL_COMPATIBILITY_FK_REFUSED",
    "AI_MODEL_COMPATIBILITY_POLICY_REFUSED",
    "AI_MODEL_CONFIG_NOT_ACCEPTED",
    "ATTACHMENT_EVIDENCE_NOT_FOUND",
    "CALLBACK_EXPIRED",
    "CALLBACK_KEY_MISSING",
    "CALLBACK_KEY_UNSAFE",
    "CALLBACK_TOKEN_INVALID",
    "CALLBACK_WRONG_ACTION",
    "FINALIZATION_LOCKED",
    "FINALIZATION_REFUSED",
    "HANDOFF_REFUSED",
    "HUMAN_ACTION_REFERENCE_INVALID",
    "HUMAN_ACTION_REFERENCE_REPLAYED",
    "IDEMPOTENCY_CONFLICT",
    "LIFECYCLE_CONFLICT",
    "OCR_EXTRACTION_FAILED",
    "PROPOSAL_TERMINAL_STATE",
    "PROPOSAL_UNAVAILABLE",
    "SNAPSHOT_AUTHORIZATION_REQUIRED",
    "STAGING_REFUSED",
    "STALE_CONTENT_HASH",
    "STALE_SNAPSHOT",
    "STALE_VERSION",
  ])],
  [7, new Set(["DEADLINE_EXCEEDED"])],
  [8, new Set([
    "AI_MODEL_COMPATIBILITY_INTERNAL",
    "HANDOFF_REFUSED",
    "INTERNAL_ERROR",
    "RESPONSE_TOO_LARGE",
  ])],
]);

const ALWAYS_RETRYABLE_BRIDGE_ERRORS = new Set([
  "FINALIZATION_LOCKED",
  "INTERNAL_ERROR",
  "STAGING_REFUSED",
  "WORKSPACE_MISSING",
]);

function isTrustedBridgeRefusal(
  exitCode: number,
  response: Extract<BridgeResponse, { status: "error" }>,
): boolean {
  const allowedCodes = BRIDGE_ERROR_CODES_BY_EXIT.get(exitCode);
  if (allowedCodes?.has(response.error.code) !== true) return false;
  if (response.error.code === "DEADLINE_EXCEEDED") return true;
  return response.error.retryable === ALWAYS_RETRYABLE_BRIDGE_ERRORS.has(response.error.code);
}

export type BridgeProcessFailureCategory =
  | "BRIDGE_PROCESS_ABORTED"
  | "BRIDGE_PROCESS_IO_FAILURE"
  | "BRIDGE_PROCESS_NONZERO_UNVERIFIED"
  | "BRIDGE_PROCESS_PROTOCOL_INVALID"
  | "BRIDGE_PROCESS_RESOURCE_LIMIT"
  | "BRIDGE_PROCESS_STARTUP_FAILED"
  | "BRIDGE_PROCESS_TIMEOUT";

class BridgeProcessFailure extends Error {
  constructor(readonly category: BridgeProcessFailureCategory) {
    super("Bridge process failed safely.");
  }
}

function processFailure(category: BridgeProcessFailureCategory): BridgeProcessFailure {
  return new BridgeProcessFailure(category);
}

export function bridgeProcessFailureCategory(error: unknown): BridgeProcessFailureCategory | undefined {
  return error instanceof BridgeProcessFailure ? error.category : undefined;
}

export interface ChildProcessLike {
  stdin: Writable;
  stdout: Readable;
  stderr: Readable;
  once(event: "close", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
  once(event: "error", listener: (error: Error) => void): this;
  kill(signal: NodeJS.Signals): boolean;
}

export type SpawnProcess = (
  executable: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcessLike;

export type RevalidatePythonExecutable = (config: FinanceBridgeConfig) => Promise<void>;

async function revalidateRuntimeForSpawn(config: FinanceBridgeConfig): Promise<void> {
  await revalidatePythonExecutableForSpawn(config);
  await verifyCoreDistributionV1(config);
}

const spawnProcess: SpawnProcess = (executable, args, options) =>
  nodeSpawn(executable, args, options) as ChildProcessLike;

function appendBounded(chunks: Buffer[], chunk: Buffer, current: number, maximum: number): number {
  const next = current + chunk.byteLength;
  if (next <= maximum) chunks.push(chunk);
  return next;
}

export class BridgeCliRunner {
  private poisoned = false;

  constructor(
    private readonly config: FinanceBridgeConfig,
    private readonly spawn: SpawnProcess = spawnProcess,
    private readonly reapGraceMs = DEFAULT_REAP_GRACE_MS,
    private readonly markUnhealthy: () => void = () => undefined,
    private readonly revalidateExecutable: RevalidatePythonExecutable =
      revalidateRuntimeForSpawn,
  ) {}

  async run(request: BridgeRequest, deadlineMs: number, inheritedFd?: number): Promise<BridgeResponse> {
    if (this.poisoned) {
      throw processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT");
    }
    if (!Number.isInteger(deadlineMs) || deadlineMs <= 0 || deadlineMs > 105_000) {
      throw new Error("Bridge deadline must be an integer from 1 through 105000 milliseconds.");
    }
    if (inheritedFd !== undefined && (!Number.isSafeInteger(inheritedFd) || inheritedFd < 0)) {
      throw new Error("Bridge inherited descriptor is invalid.");
    }
    if (!Number.isSafeInteger(this.reapGraceMs) || this.reapGraceMs <= 0 ||
        this.reapGraceMs > DEFAULT_REAP_GRACE_MS) {
      throw new Error("Bridge reap grace is invalid.");
    }
    const expiresAt = performance.now() + deadlineMs;
    let validationTimer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([
        this.revalidateExecutable(this.config),
        new Promise<never>((_resolve, reject) => {
          validationTimer = setTimeout(
            () => reject(processFailure("BRIDGE_PROCESS_TIMEOUT")),
            deadlineMs,
          );
        }),
      ]);
    } catch (error) {
      if (error instanceof BridgeProcessFailure &&
          error.category === "BRIDGE_PROCESS_TIMEOUT") {
        throw error;
      }
      throw processFailure("BRIDGE_PROCESS_STARTUP_FAILED");
    } finally {
      if (validationTimer !== undefined) clearTimeout(validationTimer);
    }
    const input = serializeBridgeRequest(request);
    if (performance.now() >= expiresAt) {
      throw processFailure("BRIDGE_PROCESS_TIMEOUT");
    }
    const child = this.spawn(
      this.config.pythonExecutable,
      ["-I", "-B", "-c", CORE_CLI_BOOTSTRAP, this.config.coreDistributionRoot],
      {
        cwd: this.config.coreDistributionRoot,
        detached: false,
        env: {
          FINANCE_RUNTIME_ROOT: this.config.repoRoot,
          LANG: "C.UTF-8",
          LC_ALL: "C.UTF-8",
          PYTHONDONTWRITEBYTECODE: "1",
          PYTHONNOUSERSITE: "1",
          PYTHONUTF8: "1",
        },
        shell: false,
        stdio: inheritedFd === undefined
          ? ["pipe", "pipe", "pipe"]
          : ["pipe", "pipe", "pipe", inheritedFd],
      },
    );

    return await new Promise<BridgeResponse>((resolve, reject) => {
      const stdout: Buffer[] = [];
      let stdoutBytes = 0;
      let terminalError: Error | undefined;
      let settled = false;
      let killTimer: NodeJS.Timeout | undefined;
      let reapTimer: NodeJS.Timeout | undefined;

      const clearTimers = (): void => {
        clearTimeout(timer);
        if (killTimer !== undefined) clearTimeout(killTimer);
        if (reapTimer !== undefined) clearTimeout(reapTimer);
      };

      const terminate = (error: Error): void => {
        if (terminalError !== undefined || settled) return;
        terminalError = error;
        child.kill("SIGTERM");
        killTimer = setTimeout(() => {
          if (settled) return;
          child.kill("SIGKILL");
          reapTimer = setTimeout(() => {
            if (settled) return;
            settled = true;
            this.poisoned = true;
            this.markUnhealthy();
            clearTimers();
            reject(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
          }, this.reapGraceMs);
        }, this.reapGraceMs);
      };

      const timer = setTimeout(() => {
        terminate(processFailure("BRIDGE_PROCESS_TIMEOUT"));
      }, Math.max(0, Math.floor(expiresAt - performance.now())));

      child.stdout.on("data", (value: Buffer | string) => {
        const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
        stdoutBytes = appendBounded(stdout, chunk, stdoutBytes, MAX_RESPONSE_BYTES);
        if (stdoutBytes > MAX_RESPONSE_BYTES) {
          terminate(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
        }
      });
      child.stderr.on("data", (value: Buffer | string) => {
        // Drain diagnostics so the pipe cannot block. Stderr is never trusted,
        // retained, or included in an exception returned to the caller.
        void value;
      });
      child.stdin.on("error", (error) => {
        if (settled) return;
        void error;
        terminate(processFailure("BRIDGE_PROCESS_IO_FAILURE"));
      });
      child.once("error", (error) => {
        if (settled) return;
        settled = true;
        clearTimers();
        void error;
        reject(processFailure("BRIDGE_PROCESS_STARTUP_FAILED"));
      });
      child.once("close", (code, signal) => {
        if (settled) return;
        settled = true;
        clearTimers();
        if (terminalError !== undefined) {
          reject(terminalError);
          return;
        }
        if (signal !== null || code === null) {
          reject(processFailure("BRIDGE_PROCESS_ABORTED"));
          return;
        }
        let response: BridgeResponse;
        try {
          response = parseBridgeResponse(Buffer.concat(stdout), request);
        } catch {
          reject(processFailure(
            code !== 0 && stdoutBytes === 0
              ? "BRIDGE_PROCESS_NONZERO_UNVERIFIED"
              : "BRIDGE_PROCESS_PROTOCOL_INVALID",
          ));
          return;
        }
        if (code === 0) {
          if (response.status !== "ok" ||
              response.operationId !== expectedBridgeOperationId(request)) {
            reject(processFailure("BRIDGE_PROCESS_PROTOCOL_INVALID"));
            return;
          }
          resolve(response);
          return;
        }
        if (response.status !== "error" ||
            response.requestId !== request.request_id ||
            response.operationId !== expectedBridgeOperationId(request) ||
            !isTrustedBridgeRefusal(code, response)) {
          reject(processFailure("BRIDGE_PROCESS_NONZERO_UNVERIFIED"));
          return;
        }
        const safeDetails = safeModelEvaluationRefusalDetailsV1(response.error);
        resolve({
          ...response,
          error: {
            code: response.error.code,
            message: "Bridge command was refused.",
            retryable: response.error.retryable,
            ...(safeDetails === undefined ? {} : { details: safeDetails }),
          },
        });
      });

      child.stdin.end(input);
    });
  }
}
