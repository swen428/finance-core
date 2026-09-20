import assert from "node:assert/strict";
import { execFile as execFileCallback, spawn } from "node:child_process";
import { EventEmitter, once } from "node:events";
import {
  chmod,
  mkdtemp,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";
import { promisify } from "node:util";

import { validatePluginConfig } from "../src/config.js";
import {
  MAX_REQUEST_BYTES,
  MAX_RESPONSE_BYTES,
  MAX_ENVELOPE_DEPTH,
  MAX_IDEMPOTENCY_KEY_LENGTH,
  ENVELOPE_VERSION,
  MODEL_EVALUATION_VERIFICATION_REASONS_V1,
  canonicalCaptureKey,
  captureIdentities,
  createBridgeRequest,
  expectedBridgeOperationId,
  parseBridgeResponse,
  safeModelEvaluationRefusalDetailsV1,
  type BridgeRequest,
} from "../src/protocol.js";
import {
  BridgeCliRunner,
  bridgeProcessFailureCategory,
  type ChildProcessLike,
  type SpawnProcess,
} from "../src/subprocess.js";
import type { FinanceDeliveryMaterialV1 } from "../src/delivery-receipt.js";

const execFile = promisify(execFileCallback);
const CURRENT_REPOSITORY_ROOT = resolve("../..");

function minimalPythonEnvironment(): NodeJS.ProcessEnv {
  return {
    FINANCE_RUNTIME_ROOT: CURRENT_REPOSITORY_ROOT,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
  };
}

let currentRepositoryPython: Promise<string> | undefined;

async function resolveCurrentRepositoryPython(): Promise<string> {
  if (currentRepositoryPython === undefined) {
    currentRepositoryPython = (async () => {
      const configured = process.env.PYTHON_EXECUTABLE;
      const python = configured === undefined || configured.length === 0
        ? join(CURRENT_REPOSITORY_ROOT, ".venv", "bin", "python")
        : configured;
      assert.ok(isAbsolute(python), "PYTHON_EXECUTABLE must be absolute");
      assert.equal(resolve(python), python, "PYTHON_EXECUTABLE must be normalized");
      const metadata = await stat(python);
      assert.ok(metadata.isFile(), "PYTHON_EXECUTABLE must resolve to a file");
      assert.notEqual(metadata.mode & 0o111, 0, "PYTHON_EXECUTABLE must be executable");
      const result = await execFile(python, [
        "-c",
        "import json,sys; print(json.dumps(list(sys.version_info[:2])))",
      ], {
        cwd: CURRENT_REPOSITORY_ROOT,
        env: minimalPythonEnvironment(),
        timeout: 5_000,
      });
      assert.deepEqual(JSON.parse(result.stdout.trim()), [3, 12]);
      return python;
    })();
  }
  return await currentRepositoryPython;
}

async function currentPythonContractProbe(): Promise<Record<string, unknown>> {
  const python = await resolveCurrentRepositoryPython();
  const program = `
import json
from finance_core.openclaw_staging_bridge import cli, envelope, identity

arguments = {"z": ["é", "😀"], "a": {"β": 1, "x": True}}
capture_key = "raw-intake:telegram:111:20"
print(json.dumps({
    "envelope_version": envelope.ENVELOPE_VERSION,
    "max_request_bytes": envelope.MAX_REQUEST_BYTES,
    "max_response_bytes": envelope.MAX_RESPONSE_BYTES,
    "max_envelope_depth": envelope.MAX_ENVELOPE_DEPTH,
    "max_idempotency_key_length": envelope.MAX_IDEMPOTENCY_KEY_LENGTH,
    "operation_id": identity.operation_id(
        "verify_ai_model_compatibility_case_v2", None, arguments
    ),
    "capture_identities": identity.capture_identities(capture_key),
    "module_files": {
        "cli": cli.__file__,
        "envelope": envelope.__file__,
        "identity": identity.__file__,
    },
}, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
`;
  const result = await execFile(python, ["-c", program], {
    cwd: CURRENT_REPOSITORY_ROOT,
    env: minimalPythonEnvironment(),
    maxBuffer: 64 * 1024,
    timeout: 10_000,
  });
  assert.equal(result.stderr, "");
  return JSON.parse(result.stdout.trim()) as Record<string, unknown>;
}

async function invokeCurrentPythonRaw(raw: Buffer): Promise<{
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: Buffer;
}> {
  const python = await resolveCurrentRepositoryPython();
  const child = spawn(
    python,
    ["-m", "finance_core.openclaw_staging_bridge.cli"],
    {
      cwd: CURRENT_REPOSITORY_ROOT,
      detached: false,
      env: minimalPythonEnvironment(),
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  return await new Promise((resolvePromise, rejectPromise) => {
    const stdout: Buffer[] = [];
    let stdoutBytes = 0;
    let terminalError: Error | undefined;
    const timer = setTimeout(() => {
      terminalError = new Error("Current Python CLI test probe timed out.");
      child.kill("SIGKILL");
    }, 10_000);
    child.stdout.on("data", (value: Buffer | string) => {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      stdoutBytes += chunk.byteLength;
      if (stdoutBytes <= MAX_RESPONSE_BYTES) {
        stdout.push(chunk);
      } else if (terminalError === undefined) {
        terminalError = new Error("Current Python CLI test probe exceeded stdout limit.");
        child.kill("SIGKILL");
      }
    });
    child.stderr.on("data", (value: Buffer | string) => {
      // Diagnostics are deliberately drained but never retained or exposed.
      void value;
    });
    child.stdin.on("error", () => {
      if (terminalError === undefined) {
        terminalError = new Error("Current Python CLI test probe stdin failed.");
        child.kill("SIGKILL");
      }
    });
    child.once("error", () => {
      clearTimeout(timer);
      rejectPromise(new Error("Current Python CLI test probe failed to start."));
    });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      if (terminalError !== undefined) {
        rejectPromise(terminalError);
        return;
      }
      resolvePromise({ code, signal, stdout: Buffer.concat(stdout) });
    });
    child.stdin.end(raw);
  });
}

test("current Python modules exactly match the TypeScript bridge contract", async () => {
  const probe = await currentPythonContractProbe();
  const request: BridgeRequest = {
    envelope_version: ENVELOPE_VERSION,
    command: "verify_ai_model_compatibility_case_v2",
    request_id: `req_${"1".repeat(32)}`,
    arguments: { z: ["é", "😀"], a: { "β": 1, x: true } },
  };
  assert.deepEqual(probe, {
    envelope_version: ENVELOPE_VERSION,
    max_request_bytes: MAX_REQUEST_BYTES,
    max_response_bytes: MAX_RESPONSE_BYTES,
    max_envelope_depth: MAX_ENVELOPE_DEPTH,
    max_idempotency_key_length: MAX_IDEMPOTENCY_KEY_LENGTH,
    operation_id: expectedBridgeOperationId(request),
    capture_identities: {
      raw_intake_public_id: captureIdentities("raw-intake:telegram:111:20").rawIntakePublicId,
      attachment_evidence_public_id:
        captureIdentities("raw-intake:telegram:111:20").attachmentEvidencePublicId,
      extraction_public_id:
        captureIdentities("raw-intake:telegram:111:20").extractionPublicId,
      proposal_public_id: captureIdentities("raw-intake:telegram:111:20").proposalPublicId,
      link_public_id: captureIdentities("raw-intake:telegram:111:20").linkPublicId,
    },
    module_files: {
      cli: join(CURRENT_REPOSITORY_ROOT, "finance_core", "openclaw_staging_bridge", "cli.py"),
      envelope: join(
        CURRENT_REPOSITORY_ROOT,
        "finance_core",
        "openclaw_staging_bridge",
        "envelope.py",
      ),
      identity: join(
        CURRENT_REPOSITORY_ROOT,
        "finance_core",
        "openclaw_staging_bridge",
        "identity.py",
      ),
    },
  });
});

test("current Python CLI returns one bounded oversized-envelope refusal", async () => {
  const outcome = await invokeCurrentPythonRaw(Buffer.alloc(MAX_REQUEST_BYTES + 1, "x"));
  assert.equal(outcome.code, 3);
  assert.equal(outcome.signal, null);
  assert.ok(outcome.stdout.byteLength <= MAX_RESPONSE_BYTES);
  const lines = outcome.stdout.toString("utf8").trim().split(/\r?\n/u);
  assert.equal(lines.length, 1);
  assert.deepEqual(JSON.parse(lines[0]!), {
    envelope_version: ENVELOPE_VERSION,
    error: {
      code: "OVERSIZED_ENVELOPE",
      message: "Request envelope exceeds the 256 KiB stdin limit.",
      retryable: false,
    },
    operation_id: null,
    request_id: null,
    status: "error",
  });
});

test("model verifier reason allowlist exactly matches Python and strips stale reasons", async () => {
  const pythonSource = await readFile(
    resolve("../../finance_core/parser_proposals/ai_model_compatibility.py"),
    "utf8",
  );
  const pythonReasons = [...pythonSource.matchAll(
    /(?:_VerificationRefusal|_verification_details)\(\s*"([A-Z0-9_]+)"/gu,
  )].map((match) => match[1]!);
  assert.deepEqual(
    [...MODEL_EVALUATION_VERIFICATION_REASONS_V1].sort(),
    [...new Set(pythonReasons)].sort(),
  );
  for (const reason of MODEL_EVALUATION_VERIFICATION_REASONS_V1) {
    assert.deepEqual(safeModelEvaluationRefusalDetailsV1({
      code: "AI_MODEL_EVAL_REFUSED",
      message: "safe refusal",
      retryable: false,
      details: { verification_reason: reason, raw_provider: "must-not-survive" },
    }), { verification_reason: reason });
  }
  assert.equal(safeModelEvaluationRefusalDetailsV1({
    code: "AI_MODEL_EVAL_REFUSED",
    message: "safe refusal",
    retryable: false,
    details: {
      verification_reason: "AMBIGUITY_FLAGS_INVALID",
      raw_provider: "must-not-survive",
    },
  }), undefined);
});

const AGENT_PROFILE_V2 = {
  openclawPackageSha256: "1".repeat(64),
  financeCommit: "2".repeat(40),
  coreVersion: "0.1.0",
  coreManifestSha256: "4".repeat(64),
  coreWheelSha256: "5".repeat(64),
  coreApiContractVersion: "finance-core-api-v1",
  coreMigrationLedgerDigest: "6".repeat(64),
  pluginBuildSha256: "3".repeat(64),
  executionClass: "cloud_projection" as const,
};
const ACCEPT_TEST_EXECUTABLE = async (): Promise<void> => undefined;

class FakeChild extends EventEmitter implements ChildProcessLike {
  readonly stdin = new PassThrough();
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly signals: NodeJS.Signals[] = [];

  kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    queueMicrotask(() => this.emit("close", null, signal));
    return true;
  }
}

test("closed delivery receipt runner invokes only the dedicated consumer module", async () => {
  const child = new FakeChild();
  let args: readonly string[] = [];
  const input: Buffer[] = [];
  child.stdin.on("data", (chunk: Buffer | string) => {
    input.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  });
  child.stdin.on("finish", () => {
    child.stdout.end(JSON.stringify({
      observation_public_id: `d2dobs_${"9".repeat(32)}`,
      status: "ok",
    }));
    child.emit("close", 0, null);
  });
  const runner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    (_executable, processArgs) => {
      args = processArgs;
      return child;
    },
    undefined,
    undefined,
    ACCEPT_TEST_EXECUTABLE,
  );
  const material: FinanceDeliveryMaterialV1 = {
    capability: "telegram.finance-delivery-material-v1",
    deliveryMaterialVersion: "finance_d2_delivery_material_v1",
    attemptNonce: `d2nonce_${"1".repeat(32)}`,
    deliveryMaterialSha256: "2".repeat(64),
    providerMessageId: "200",
    receiptTokenSha256: "3".repeat(64),
    channel: "telegram",
    accountId: "finance-account",
    conversationId: "111",
    sessionKey: "binding-1",
    sourceIdentitySha256: "4".repeat(64),
  };
  await runner.recordFinanceDeliveryReceipt(material, 1_000);
  assert.match(String(args[3]), /delivery_receipt_cli/u);
  const payload = JSON.parse(Buffer.concat(input).toString("utf8")) as Record<string, unknown>;
  assert.equal(payload.workspace_path, "/tmp/workspace");
  assert.equal(payload.attempt_nonce, material.attemptNonce);
  assert.equal(payload.delivery_material_sha256, material.deliveryMaterialSha256);
  assert.equal("command" in payload, false);
  assert.equal("idempotency_key" in payload, false);
});

type PythonFixtureMode =
  | "error"
  | "ai-model-eval-details"
  | "ok"
  | "ok-wrong-operation"
  | "wrong-request"
  | "wrong-operation"
  | "wrong-exit"
  | "wrong-retryable"
  | "workspace-missing"
  | "handoff-validation"
  | "handoff-authority"
  | "handoff-internal"
  | "deadline-retryable"
  | "deadline-nonretryable"
  | "staging-refused"
  | "staging-wrong-retryable"
  | "unknown-code"
  | "protocol"
  | "bad-json"
  | "late";

let pythonExecutable: Promise<string> | undefined;

async function resolvePythonExecutable(): Promise<string> {
  if (pythonExecutable === undefined) {
    pythonExecutable = (async () => {
      const configured = process.env.PYTHON_EXECUTABLE ?? process.env.PYTHON;
      if (configured !== undefined && configured.length > 0) return configured;
      const result = await execFile("python3", ["-c", "import sys; print(sys.executable)"]);
      return result.stdout.trim();
    })();
  }
  return await pythonExecutable;
}

async function writePythonCliFixture(
  mode: PythonFixtureMode,
  lateOutputMarker: string | undefined = undefined,
): Promise<{ root: string; python: string }> {
  const root = await mkdtemp(join(tmpdir(), "finance-bridge-envelope-"));
  const financeCoreRoot = join(root, "finance_core");
  const packageRoot = join(financeCoreRoot, "openclaw_staging_bridge");
  await mkdir(packageRoot, { recursive: true });
  await writeFile(join(financeCoreRoot, "__init__.py"), "", { mode: 0o600 });
  await writeFile(join(packageRoot, "__init__.py"), "", { mode: 0o600 });
  const modeLiteral = JSON.stringify(mode);
  const markerLiteral = JSON.stringify(lateOutputMarker ?? "");
  await writeFile(join(packageRoot, "cli.py"), `
import hashlib
import json
import signal
import sys
import threading
import time

mode = ${modeLiteral}
request = json.loads(sys.stdin.read())
arguments_json = json.dumps(request["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
operation_id = "op_" + hashlib.sha256(
    "\\x00".join(("operation", request["command"], request.get("idempotency_key") or "", arguments_json)).encode("utf-8")
).hexdigest()[:32]

if mode == "late":
    termination_seen = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: termination_seen.set())
    sys.stderr.write("fixture-ready\\n")
    sys.stderr.flush()
    if not termination_seen.wait(5):
        raise RuntimeError("fixture did not receive deadline termination")
    with open(${markerLiteral}, "w", encoding="utf-8") as marker:
        marker.write("late-output")
if mode == "bad-json":
    sys.stdout.write("{{not-json}}\\n")
    sys.stdout.flush()
    raise SystemExit(4)

request_id = request["request_id"]
if mode == "wrong-request":
    request_id = "req_" + "0" * 32
if mode in {"wrong-operation", "ok-wrong-operation"}:
    operation_id = "op_" + "0" * 32

if mode in {"ok", "ok-wrong-operation"}:
    response = {
        "envelope_version": "v1",
        "request_id": request_id,
        "operation_id": operation_id,
        "status": "ok",
        "result": {"healthy": True},
        "idempotent_replay": False,
    }
else:
    error_contract = {
        "wrong-exit": ("UNKNOWN_COMMAND", False, 8),
        "wrong-retryable": ("UNKNOWN_COMMAND", True, 4),
        "workspace-missing": ("WORKSPACE_MISSING", True, 2),
        "handoff-validation": ("HANDOFF_REFUSED", False, 5),
        "handoff-authority": ("HANDOFF_REFUSED", False, 6),
        "handoff-internal": ("HANDOFF_REFUSED", False, 8),
        "deadline-retryable": ("DEADLINE_EXCEEDED", True, 7),
        "deadline-nonretryable": ("DEADLINE_EXCEEDED", False, 7),
        "staging-refused": ("STAGING_REFUSED", True, 6),
        "staging-wrong-retryable": ("STAGING_REFUSED", False, 6),
    }
    error_code, retryable, exit_code = error_contract.get(
        mode,
        ("AI_MODEL_EVAL_REFUSED", False, 5) if mode == "ai-model-eval-details"
        else ("NOT_ALLOWLISTED" if mode == "unknown-code" else "UNKNOWN_COMMAND", False, 4),
    )
    response = {
        "envelope_version": "v0" if mode == "protocol" else "v1",
        "request_id": request_id,
        "operation_id": operation_id,
        "status": "error",
        "error": {
            "code": error_code,
            "message": "safe refusal",
            "retryable": retryable,
        },
    }
    if mode == "ai-model-eval-details":
        response["error"]["details"] = {
            "verification_reason": "FIELD_MISMATCH",
            "verification_field": "amount",
            "ignored_untrusted_detail": "sk-live-bridge-secret",
        }
sys.stderr.write("bridge-secret-from-stderr\\n")
sys.stderr.flush()
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
sys.stdout.flush()
raise SystemExit(
    0 if mode == "ok-wrong-operation"
    else 8 if mode == "ok"
    else exit_code
)
`, { mode: 0o600 });
  return { root, python: await resolvePythonExecutable() };
}

function runnerForFixture(root: string, python: string): BridgeCliRunner {
  return new BridgeCliRunner({
    repoRoot: root,
    coreDistributionRoot: root,
    pythonExecutable: python,
    workspaceRoot: join(root, "workspace"),
    agentProfileV2: AGENT_PROFILE_V2,
  }, undefined, undefined, undefined, ACCEPT_TEST_EXECUTABLE);
}

test("protocol identities exactly match Python capture_identities vectors", () => {
  const key = canonicalCaptureKey("111", "20");
  assert.equal(key, "raw-intake:telegram:111:20");
  assert.deepEqual(captureIdentities(key), {
    rawIntakePublicId: "raw_intake_bridge_686d5e5cd838efaa6565a084118bb81d",
    attachmentEvidencePublicId: "tgae_bridge_686d5e5cd838efaa6565a084118bb81d",
    extractionPublicId: "rocr_bridge_686d5e5cd838efaa6565a084118bb81d",
    proposalPublicId: "prop_bridge_686d5e5cd838efaa6565a084118bb81d",
    linkPublicId: "ropl_bridge_686d5e5cd838efaa6565a084118bb81d",
  });
  assert.equal(
    captureIdentities(canonicalCaptureKey("1", "23")).rawIntakePublicId,
    "raw_intake_bridge_58f442340b36cca6bb0e9257ca7529b4",
  );
  assert.equal(
    captureIdentities(canonicalCaptureKey("12", "3")).rawIntakePublicId,
    "raw_intake_bridge_cfd549b11c2d3c7dda683b7e54913a31",
  );
});

test("operation identity exactly matches Python canonical JSON vectors", () => {
  const request = (command: string, arguments_: BridgeRequest["arguments"], idempotencyKey?: string): BridgeRequest => ({
    envelope_version: "v1",
    command,
    request_id: `req_${"1".repeat(32)}`,
    ...(idempotencyKey === undefined ? {} : { idempotency_key: idempotencyKey }),
    arguments: arguments_,
  });
  assert.equal(
    expectedBridgeOperationId(request("health", { workspace_path: "/tmp/workspace" })),
    "op_965fe00c004f2e9a2d303086ef0361ed",
  );
  assert.equal(
    expectedBridgeOperationId(request("verify_ai_model_compatibility_case_v2", {
      z: ["é", "😀"],
      a: { "β": 1, x: true },
    })),
    "op_305c88a0ad929409eb8ff3d81639f4d7",
  );
  assert.equal(
    expectedBridgeOperationId(request("capture", { amount: 1, note: "café" }, "key-1")),
    "op_60e41443fe29f0f1f309ce7859c6ea0a",
  );
});

test("strict response parser accepts one matching v1 response and rejects extra output", () => {
  const request = createBridgeRequest("health", { workspace_path: "/tmp/workspace" });
  const response = `${JSON.stringify({
    envelope_version: "v1",
    request_id: request.request_id,
    operation_id: "op_0123456789abcdef0123456789abcdef",
    status: "ok",
    result: { healthy: true },
    idempotent_replay: false,
  })}\n`;
  assert.deepEqual(parseBridgeResponse(Buffer.from(response), request), {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: "op_0123456789abcdef0123456789abcdef",
    status: "ok",
    result: { healthy: true },
    idempotentReplay: false,
  });
  assert.throws(
    () => parseBridgeResponse(Buffer.from(`${response}{"extra":true}\n`), request),
    /exactly one JSON line/u,
  );
  assert.throws(
    () => parseBridgeResponse(Buffer.alloc(MAX_RESPONSE_BYTES + 1), request),
    /1 MiB/u,
  );
  assert.throws(
    () => parseBridgeResponse(Buffer.from([0xff, 0xfe, 0xfd]), request),
    /UTF-8/u,
  );
  assert.deepEqual(parseBridgeResponse(Buffer.from(`${JSON.stringify({
    envelope_version: "v1",
    request_id: null,
    operation_id: null,
    status: "error",
    error: { code: "MALFORMED_ENVELOPE", message: "refused", retryable: false },
  })}\n`), request), {
    envelopeVersion: "v1",
    requestId: null,
    operationId: null,
    status: "error",
    error: { code: "MALFORMED_ENVELOPE", message: "refused", retryable: false },
  });
});

test("CLI runner returns a request/operation-bound Python error envelope after nonzero exit", async () => {
  const fixture = await writePythonCliFixture("error");
  const runner = runnerForFixture(fixture.root, fixture.python);
  const request = createBridgeRequest("unknown_command", {});
  try {
    const response = await runner.run(request, 1_000);
    assert.equal(response.status, "error");
    if (response.status !== "error") throw new Error("expected a bridge failure response");
    assert.equal(response.requestId, request.request_id);
    assert.equal(response.operationId, expectedBridgeOperationId(request));
    assert.deepEqual(response.error, {
      code: "UNKNOWN_COMMAND",
      message: "Bridge command was refused.",
      retryable: false,
    });
    assert.doesNotMatch(JSON.stringify(response), /bridge-secret-from-stderr/u);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("CLI runner preserves only allowlisted Python verifier details", async () => {
  const fixture = await writePythonCliFixture("ai-model-eval-details");
  const runner = runnerForFixture(fixture.root, fixture.python);
  const request = createBridgeRequest("health", {});
  try {
    const response = await runner.run(request, 1_000);
    assert.equal(response.status, "error");
    if (response.status !== "error") throw new Error("expected a bridge failure response");
    assert.deepEqual(response.error, {
      code: "AI_MODEL_EVAL_REFUSED",
      message: "Bridge command was refused.",
      retryable: false,
      details: {
        verification_reason: "FIELD_MISMATCH",
        verification_field: "amount",
      },
    });
    assert.doesNotMatch(JSON.stringify(response), /sk-live-bridge-secret/u);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("CLI runner generically rejects nonzero success and never exposes stderr", async () => {
  const fixture = await writePythonCliFixture("ok");
  const runner = runnerForFixture(fixture.root, fixture.python);
  try {
    await assert.rejects(
      runner.run(createBridgeRequest("health", {}), 1_000),
      (error: unknown) => {
        assert.equal(
          bridgeProcessFailureCategory(error),
          "BRIDGE_PROCESS_NONZERO_UNVERIFIED",
        );
        assert.doesNotMatch(String(error), /bridge-secret-from-stderr/u);
        return true;
      },
    );
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("CLI runner rejects a successful response with the wrong operation binding", async () => {
  const fixture = await writePythonCliFixture("ok-wrong-operation");
  const runner = runnerForFixture(fixture.root, fixture.python);
  try {
    await assert.rejects(
      runner.run(createBridgeRequest("health", {}), 1_000),
      (error: unknown) =>
        bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_PROTOCOL_INVALID",
    );
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("CLI runner accepts the bound workspace-missing usage refusal", async () => {
  const fixture = await writePythonCliFixture("workspace-missing");
  const runner = runnerForFixture(fixture.root, fixture.python);
  const request = createBridgeRequest("health", {});
  try {
    const response = await runner.run(request, 1_000);
    assert.equal(response.status, "error");
    if (response.status !== "error") throw new Error("expected a bridge failure response");
    assert.equal(response.operationId, expectedBridgeOperationId(request));
    assert.deepEqual(response.error, {
      code: "WORKSPACE_MISSING",
      message: "Bridge command was refused.",
      retryable: true,
    });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("CLI runner verifies the current Python CLI workspace refusal", async () => {
  const parent = await mkdtemp(join(tmpdir(), "finance-bridge-current-"));
  const missingWorkspace = join(parent, "缺失-workspace");
  const python = await resolveCurrentRepositoryPython();
  const request: BridgeRequest = {
    envelope_version: ENVELOPE_VERSION,
    command: "health",
    request_id: `req_${"a".repeat(32)}`,
    arguments: { workspace_path: missingWorkspace },
  };
  const runner = new BridgeCliRunner({
    repoRoot: CURRENT_REPOSITORY_ROOT,
    coreDistributionRoot: CURRENT_REPOSITORY_ROOT,
    pythonExecutable: python,
    workspaceRoot: missingWorkspace,
    agentProfileV2: AGENT_PROFILE_V2,
  }, undefined, undefined, undefined, ACCEPT_TEST_EXECUTABLE);
  try {
    const response = await runner.run(request, 10_000);
    assert.equal(response.envelopeVersion, ENVELOPE_VERSION);
    assert.equal(response.requestId, request.request_id);
    assert.equal(response.operationId, expectedBridgeOperationId(request));
    assert.equal(response.status, "error");
    if (response.status !== "error") throw new Error("expected a bridge failure response");
    assert.deepEqual(response.error, {
      code: "WORKSPACE_MISSING",
      message: "Bridge command was refused.",
      retryable: true,
    });
    const publicResponse = JSON.stringify(response);
    assert.doesNotMatch(publicResponse, /缺失-workspace/u);
    assert.doesNotMatch(publicResponse, /database|credential|provider|stderr/u);
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

test("CLI runner accepts production multi-exit and retryability contracts", async () => {
  for (const [mode, expected] of [
    ["handoff-validation", ["HANDOFF_REFUSED", false]],
    ["handoff-authority", ["HANDOFF_REFUSED", false]],
    ["handoff-internal", ["HANDOFF_REFUSED", false]],
    ["deadline-retryable", ["DEADLINE_EXCEEDED", true]],
    ["deadline-nonretryable", ["DEADLINE_EXCEEDED", false]],
    ["staging-refused", ["STAGING_REFUSED", true]],
  ] as const) {
    const fixture = await writePythonCliFixture(mode);
    const runner = runnerForFixture(fixture.root, fixture.python);
    try {
      const response = await runner.run(createBridgeRequest("health", {}), 1_000);
      assert.equal(response.status, "error");
      if (response.status !== "error") throw new Error("expected a bridge failure response");
      assert.equal(response.error.code, expected[0], mode);
      assert.equal(response.error.retryable, expected[1], mode);
    } finally {
      await rm(fixture.root, { recursive: true, force: true });
    }
  }
});

test("CLI runner rejects mismatched exit, error code, or retryability tuples", async () => {
  for (const mode of [
    "wrong-exit",
    "wrong-retryable",
    "staging-wrong-retryable",
  ] as const) {
    const fixture = await writePythonCliFixture(mode);
    const runner = runnerForFixture(fixture.root, fixture.python);
    try {
      await assert.rejects(
        runner.run(createBridgeRequest("health", {}), 1_000),
        (error: unknown) =>
          bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_NONZERO_UNVERIFIED",
      );
    } finally {
      await rm(fixture.root, { recursive: true, force: true });
    }
  }
});

test("CLI runner rejects wrong bindings, unknown codes, protocol, and bad JSON", async () => {
  for (const [mode, expectedCategory] of [
    ["wrong-request", "BRIDGE_PROCESS_PROTOCOL_INVALID"],
    ["wrong-operation", "BRIDGE_PROCESS_NONZERO_UNVERIFIED"],
    ["unknown-code", "BRIDGE_PROCESS_NONZERO_UNVERIFIED"],
    ["protocol", "BRIDGE_PROCESS_PROTOCOL_INVALID"],
    ["bad-json", "BRIDGE_PROCESS_PROTOCOL_INVALID"],
  ] as const) {
    const fixture = await writePythonCliFixture(mode);
    const runner = runnerForFixture(fixture.root, fixture.python);
    try {
      await assert.rejects(
        runner.run(createBridgeRequest("health", {}), 1_000),
        (error: unknown) => {
          assert.equal(bridgeProcessFailureCategory(error), expectedCategory, mode);
          assert.doesNotMatch(String(error), /bridge-secret-from-stderr/u);
          return true;
        },
      );
    } finally {
      await rm(fixture.root, { recursive: true, force: true });
    }
  }
});

test("CLI runner rejects a timeout even when real Python writes a late response", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "finance-bridge-envelope-late-"));
  const marker = join(root, "late-output.marker");
  const fixture = await writePythonCliFixture("late", marker);
  let child: ChildProcessLike | undefined;
  let ready: Promise<unknown[]> | undefined;
  let markSpawned!: () => void;
  const spawned = new Promise<void>((resolve) => { markSpawned = resolve; });
  const runner = new BridgeCliRunner({
    repoRoot: fixture.root,
    coreDistributionRoot: fixture.root,
    pythonExecutable: fixture.python,
    workspaceRoot: join(fixture.root, "workspace"),
    agentProfileV2: AGENT_PROFILE_V2,
  }, (executable, args, options) => {
    const process = spawn(executable, args, options);
    child = process as ChildProcessLike;
    ready = once(child.stderr, "data", { signal: AbortSignal.timeout(5_000) });
    markSpawned();
    return child;
  }, undefined, undefined, ACCEPT_TEST_EXECUTABLE);
  // Keep the actual deadline and real SIGTERM/response path, but do not make
  // the fixture's readiness depend on Python cold-start speed.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const refused = assert.rejects(
      runner.run(createBridgeRequest("health", {}), 100),
      (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_TIMEOUT",
    );
    await spawned;
    assert.ok(ready);
    const [message] = await ready;
    assert.equal(String(message), "fixture-ready\n");
    t.mock.timers.tick(100);
    await refused;
    assert.equal(await readFile(marker, "utf8"), "late-output");
  } finally {
    t.mock.timers.reset();
    child?.kill("SIGKILL");
    await rm(fixture.root, { recursive: true, force: true });
    await rm(root, { recursive: true, force: true });
  }
});

test("CLI runner classifies an unexpected child signal as aborted", async () => {
  const child = new FakeChild();
  const runner = new BridgeCliRunner({
    repoRoot: "/repo",
    coreDistributionRoot: "/core-distribution",
    pythonExecutable: "/usr/bin/python3",
    workspaceRoot: "/workspace",
    agentProfileV2: AGENT_PROFILE_V2,
  }, () => {
    queueMicrotask(() => child.emit("close", null, "SIGTERM"));
    return child;
  }, undefined, undefined, ACCEPT_TEST_EXECUTABLE);
  await assert.rejects(
    runner.run(createBridgeRequest("health", {}), 1_000),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_ABORTED",
  );
});

test("CLI runner refuses a replaced Python executable before spawn", async () => {
  const root = await mkdtemp(join(tmpdir(), "finance-python-identity-"));
  try {
    const repoRoot = join(root, "repo");
    const coreDistributionRoot = join(root, "core-distribution");
    const workspaceRoot = join(root, "workspace");
    const pythonBin = join(root, "python-environment", "bin");
    await mkdir(repoRoot, { mode: 0o700 });
    await mkdir(coreDistributionRoot, { mode: 0o700 });
    await mkdir(workspaceRoot, { mode: 0o700 });
    await mkdir(pythonBin, { recursive: true, mode: 0o700 });
    const canonicalPythonBin = await realpath(pythonBin);
    const python = join(canonicalPythonBin, "python");
    await writeFile(python, "python", { mode: 0o700 });
    await chmod(python, 0o700);
    const config = await validatePluginConfig({
      repoRoot: await realpath(repoRoot),
      coreDistributionRoot: await realpath(coreDistributionRoot),
      pythonExecutable: python,
      workspaceRoot: await realpath(workspaceRoot),
      agentProfileV2: AGENT_PROFILE_V2,
    });
    await rename(python, `${python}-old`);
    await writeFile(python, "replacement", { mode: 0o700 });
    let spawnCalls = 0;
    const runner = new BridgeCliRunner(config, () => {
      spawnCalls += 1;
      return new FakeChild();
    });

    await assert.rejects(
      runner.run(createBridgeRequest("health", {}), 1_000),
      (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_STARTUP_FAILED",
    );
    assert.equal(spawnCalls, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("CLI runner charges executable revalidation to the command deadline", async () => {
  const child = new FakeChild();
  let spawnCalls = 0;
  const runner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => {
      spawnCalls += 1;
      queueMicrotask(() => child.emit("close", null, "SIGTERM"));
      return child;
    },
    undefined,
    undefined,
    async () => await new Promise<void>((resolve) => setTimeout(resolve, 20)),
  );

  await assert.rejects(
    runner.run(createBridgeRequest("health", {}), 5),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_TIMEOUT",
  );
  await new Promise<void>((resolve) => setTimeout(resolve, 25));
  assert.equal(spawnCalls, 0);
});

test("CLI runner times out a revalidation that never completes without spawning", async () => {
  let spawnCalls = 0;
  const runner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => {
      spawnCalls += 1;
      return new FakeChild();
    },
    undefined,
    undefined,
    async () => await new Promise<void>(() => undefined),
  );

  await assert.rejects(
    runner.run(createBridgeRequest("health", {}), 5),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_TIMEOUT",
  );
  assert.equal(spawnCalls, 0);
});

test("CLI runner gives the child only the deadline remaining after revalidation", async () => {
  const child = new FakeChild();
  const request = createBridgeRequest("health", {});
  let spawnCalls = 0;
  const runner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => {
      spawnCalls += 1;
      setTimeout(() => {
        child.stdout.end(`${JSON.stringify({
          envelope_version: "v1",
          request_id: request.request_id,
          operation_id: expectedBridgeOperationId(request),
          status: "ok",
          result: { healthy: true },
          idempotent_replay: false,
        })}\n`);
        child.stderr.end();
        child.emit("close", 0, null);
      }, 70);
      return child;
    },
    undefined,
    undefined,
    async () => await new Promise<void>((resolve) => setTimeout(resolve, 30)),
  );

  await assert.rejects(
    runner.run(request, 80),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_TIMEOUT",
  );
  assert.equal(spawnCalls, 1);
  assert.deepEqual(child.signals, ["SIGTERM"]);
});

test("CLI runner uses absolute fixed argv, no shell, bounded stdin, and minimal environment", async () => {
  const child = new FakeChild();
  let invocation: Parameters<SpawnProcess> | undefined;
  const request = createBridgeRequest("health", { workspace_path: "/tmp/workspace" });
  // Keep the fake response bound to the exact request generated above.
  const originalRequestId = request.request_id;
  child.removeAllListeners();
  const exactSpawn: SpawnProcess = (...args) => {
    invocation = args;
    child.stdin.on("data", () => undefined);
    queueMicrotask(() => {
      child.stdout.end(
        `${JSON.stringify({
          envelope_version: "v1",
          request_id: originalRequestId,
          operation_id: expectedBridgeOperationId(request),
          status: "ok",
          result: { healthy: true },
          idempotent_replay: false,
        })}\n`,
      );
      child.stderr.end();
      child.emit("close", 0, null);
    });
    return child;
  };
  const runner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    exactSpawn,
    undefined,
    undefined,
    ACCEPT_TEST_EXECUTABLE,
  );
  const result = await runner.run(request, 1_000, 42);

  assert.equal(result.status, "ok");
  assert.deepEqual(invocation?.[0], "/repo/.venv/bin/python");
  assert.equal(invocation?.[1][0], "-I");
  assert.equal(invocation?.[1][1], "-B");
  assert.equal(invocation?.[1][2], "-c");
  assert.match(invocation?.[1][3] ?? "", /runpy\.run_module/u);
  assert.equal(invocation?.[1][4], "/core-distribution");
  assert.equal(invocation?.[2].cwd, "/core-distribution");
  assert.equal(invocation?.[2].shell, false);
  assert.equal(invocation?.[2].detached, false);
  assert.deepEqual(invocation?.[2].stdio, ["pipe", "pipe", "pipe", 42]);
  assert.deepEqual(invocation?.[2].env, {
    FINANCE_RUNTIME_ROOT: "/repo",
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
  });

  const oversized = createBridgeRequest("capture", {
    workspace_path: "/tmp/workspace",
    blob: "x".repeat(MAX_REQUEST_BYTES),
  }, "raw-intake:telegram:111:20");
  await assert.rejects(runner.run(oversized, 1_000), /256 KiB/u);
});

test("CLI runner ignores a hostile runtime checkout that shadows finance_core", async () => {
  const fixture = await writePythonCliFixture("workspace-missing");
  const hostileRuntime = await mkdtemp(join(tmpdir(), "finance-hostile-runtime-"));
  const hostilePackage = join(hostileRuntime, "finance_core/openclaw_staging_bridge");
  await mkdir(hostilePackage, { recursive: true });
  await writeFile(join(hostileRuntime, "finance_core/__init__.py"), "raise SystemExit(91)\n");
  await writeFile(join(hostilePackage, "__init__.py"), "");
  await writeFile(join(hostilePackage, "cli.py"), "raise SystemExit(92)\n");
  const runner = new BridgeCliRunner({
    repoRoot: hostileRuntime,
    coreDistributionRoot: fixture.root,
    pythonExecutable: fixture.python,
    workspaceRoot: join(hostileRuntime, "workspace"),
    agentProfileV2: AGENT_PROFILE_V2,
  }, undefined, undefined, undefined, ACCEPT_TEST_EXECUTABLE);
  try {
    const response = await runner.run(createBridgeRequest("health", {}), 1_000);
    assert.equal(response.status, "error");
    if (response.status !== "error") throw new Error("expected a bridge refusal");
    assert.equal(response.error.code, "WORKSPACE_MISSING");
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
    await rm(hostileRuntime, { recursive: true, force: true });
  }
});

test("CLI runner rejects oversized stdout and reaps a timed-out child", async () => {
  const oversizedChild = new FakeChild();
  const oversizedRunner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => {
      queueMicrotask(() => {
        oversizedChild.stdout.end(Buffer.alloc(MAX_RESPONSE_BYTES + 1));
        oversizedChild.emit("close", 0, null);
      });
      return oversizedChild;
    },
    undefined,
    undefined,
    ACCEPT_TEST_EXECUTABLE,
  );
  await assert.rejects(
    oversizedRunner.run(createBridgeRequest("health", { workspace_path: "/tmp/workspace" }), 1_000),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_RESOURCE_LIMIT",
  );

  const timedOutChild = new FakeChild();
  const timedOutRunner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => timedOutChild,
    undefined,
    undefined,
    ACCEPT_TEST_EXECUTABLE,
  );
  await assert.rejects(
    timedOutRunner.run(createBridgeRequest("health", { workspace_path: "/tmp/workspace" }), 5),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_TIMEOUT",
  );
  assert.deepEqual(timedOutChild.signals, ["SIGTERM"]);

  const unreapedChild = new FakeChild();
  unreapedChild.kill = function kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    return true;
  };
  let markedUnhealthy = 0;
  const poisonedRunner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => unreapedChild,
    5,
    () => { markedUnhealthy += 1; },
    ACCEPT_TEST_EXECUTABLE,
  );
  const healthRequest = createBridgeRequest("health", { workspace_path: "/tmp/workspace" });
  await assert.rejects(
    poisonedRunner.run(healthRequest, 5),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_RESOURCE_LIMIT",
  );
  assert.equal(markedUnhealthy, 1);
  assert.deepEqual(unreapedChild.signals, ["SIGTERM", "SIGKILL"]);
  await assert.rejects(
    poisonedRunner.run(healthRequest, 5),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_RESOURCE_LIMIT",
  );

  const brokenStdinChild = new FakeChild();
  const brokenStdinRunner = new BridgeCliRunner(
    {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/tmp/workspace",
      agentProfileV2: AGENT_PROFILE_V2,
    },
    () => {
      queueMicrotask(() => brokenStdinChild.stdin.emit("error", new Error("EPIPE")));
      return brokenStdinChild;
    },
    undefined,
    undefined,
    ACCEPT_TEST_EXECUTABLE,
  );
  await assert.rejects(
    brokenStdinRunner.run(healthRequest, 1_000),
    (error: unknown) => bridgeProcessFailureCategory(error) === "BRIDGE_PROCESS_IO_FAILURE",
  );
  assert.deepEqual(brokenStdinChild.signals, ["SIGTERM"]);
});
