import { createHash } from "node:crypto";
import { execFile as execFileCallback } from "node:child_process";
import { readFile, realpath } from "node:fs/promises";
import { isAbsolute, join, relative, resolve } from "node:path";
import { promisify } from "node:util";

import {
  canonicalProjectionSha256V2,
  type FinanceAgentConfigProjectionV2,
} from "./agent-profile-projection-v2.js";
import { computeArtifactHashV1, type ArtifactHashResultV1 } from "./artifact-hash-v1.js";
import {
  revalidatePythonExecutableForSpawn,
  type FinanceBridgeConfig,
} from "./config.js";
import type { BridgeRunner } from "./controller.js";
import {
  verifyCoreDistributionV1,
  type CoreDistributionEvidenceV1,
} from "./core-distribution-v1.js";
import {
  createBridgeRequest,
  MODEL_EVALUATION_VERIFICATION_REASONS_V1,
  safeModelEvaluationRefusalDetailsV1,
  type BridgeResponse,
  type JsonObject,
  type JsonValue,
} from "./protocol.js";
import { bridgeProcessFailureCategory } from "./subprocess.js";

const execFile = promisify(execFileCallback);
const FIXTURE_CASE_IDS = [
  "clear_text",
  "clear_ocr",
  "ambiguous_amount_currency",
  "ocr_prompt_injection",
] as const;
const PURPOSE = "finance-bridge.ai-proposal-v2";
const AGENT_ID = "finance";
const CASE_DEADLINE_MS = 30_000;
export const COMPATIBILITY_CASE_DEADLINE_MS = CASE_DEADLINE_MS;
const MAX_RESPONSE_BYTES = 16_384;
const MAX_CATALOG_ENTRIES = 64;
const MAX_SEGMENT_BYTES = 4_096;
const MAX_SOURCE_BYTES = 24_576;
const MAX_REQUEST_BYTES = 65_536;
const PROMPT_SHA256 = "48f4d53ba802569bfe929b24d9b83e309851f8bfc18e324e8f0f5e2bd09f4644";
const FIXTURE_SHA256 = "5964e0465428ac9fece2177544410a1ad4777acc3330a60a79ed1badd7d1b6b3";

export const OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION =
  "finance-compatibility-failure-envelope-v1";

export const OPERATOR_FAILURE_CATEGORIES = [
  "AI_MODEL_EVAL_REFUSED",
  "AUTH_UNAVAILABLE",
  "ARTIFACT_REFUSED",
  "BRIDGE_REFUSED",
  "COMPATIBILITY_REFUSED",
  "CONFIG_REFUSED",
  "REHEARSAL_TIMEOUT",
  "OPERATOR_REFUSED",
] as const;

export type OperatorFailureCategoryV1 = typeof OPERATOR_FAILURE_CATEGORIES[number];

export const OPERATOR_VERIFICATION_REASONS = MODEL_EVALUATION_VERIFICATION_REASONS_V1;

export type OperatorVerificationReasonV1 = typeof OPERATOR_VERIFICATION_REASONS[number];

export const OPERATOR_FAILURE_PHASES = [
  "preflight",
  "artifact",
  "config",
  "health",
  "compatibility_case",
  "python_verification",
  "receipt_registration",
  "rehearsal_register",
  "unknown",
] as const;

export type OperatorFailurePhaseV1 = typeof OPERATOR_FAILURE_PHASES[number];

export const OPERATOR_TIMER_LAYERS = [
  "none",
  "compatibility_case",
  "rehearsal_register",
] as const;

export type OperatorTimerLayerV1 = typeof OPERATOR_TIMER_LAYERS[number];

export interface OperatorFailureEnvelopeV1 {
  schema_version: typeof OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION;
  category: OperatorFailureCategoryV1;
  phase: OperatorFailurePhaseV1;
  timer_layer: OperatorTimerLayerV1;
  elapsed_ms: number;
  timeout_triggered: boolean;
  verification_reason?: OperatorVerificationReasonV1;
  verification_field?: string;
}

export class OperatorFailureV1 extends Error {
  constructor(
    message: string,
    readonly envelope: OperatorFailureEnvelopeV1,
  ) {
    super(message);
    this.name = "OperatorFailureV1";
  }
}

export function operatorFailureEnvelopeV1(error: unknown): OperatorFailureEnvelopeV1 {
  if (error instanceof OperatorFailureV1) return error.envelope;
  return {
    schema_version: OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION,
    category: "OPERATOR_REFUSED",
    phase: "unknown",
    timer_layer: "none",
    elapsed_ms: 0,
    timeout_triggered: false,
  };
}

export function createOperatorFailureV1(
  message: string,
  details: Omit<OperatorFailureEnvelopeV1, "schema_version">,
): OperatorFailureV1 {
  return new OperatorFailureV1(message, {
    schema_version: OPERATOR_FAILURE_ENVELOPE_SCHEMA_VERSION,
    ...details,
  });
}

function safeVerificationDetails(error: BridgeResponse & { status: "error" }):
  Pick<OperatorFailureEnvelopeV1, "verification_reason" | "verification_field"> {
  const details = safeModelEvaluationRefusalDetailsV1(error.error);
  if (details === undefined) return {};
  const reason = details.verification_reason as OperatorVerificationReasonV1;
  const field = details.verification_field;
  return {
    verification_reason: reason,
    ...(typeof field === "string" ? { verification_field: field } : {}),
  };
}

export interface CompatibilityCaseV1 {
  case_id: string;
  source_kind: "telegram_text" | "receipt_local_ocr_text";
  catalog: Record<string, string>;
  parent_payload: Record<string, unknown>;
  ocr_layout: Record<string, unknown> | null;
  expected: Record<string, unknown>;
  forbidden_output_fragments: string[];
}

export interface CompatibilityFixtureSetV1 {
  version: string;
  cases: CompatibilityCaseV1[];
}

export interface CompatibilityAssetsV1 {
  prompt: string;
  fixtures: CompatibilityFixtureSetV1;
  identity_sha256: string;
}

export interface CompatibilityLlmResultV1 {
  text: string;
  provider: string;
  model: string;
  agentId: string;
  usage: Record<string, unknown>;
  audit: {
    caller: { kind: string; id?: string; name?: string };
    purpose?: string;
    sessionKey?: string;
  };
}

export interface CompatibilityLlmRuntimeV1 {
  capabilities?: { maxRetries?: unknown };
  complete(params: {
    messages: Array<{ role: "user"; content: string }>;
    model: string;
    maxTokens: number;
    temperature: 0;
    systemPrompt: string;
    purpose: string;
    agentId: "finance";
    maxRetries: 0;
    signal: AbortSignal;
  }): Promise<CompatibilityLlmResultV1>;
}

export interface CompatibilityHarnessOutcomeV1 {
  case_id: string;
  ordinary_agent_turn_count: 0;
  isolated_completion_count: 1;
  provider_dispatch_count: 1;
  effective_max_retries: 0;
  elapsed_ms: number;
  observed_provider: string;
  observed_model: string;
  observed_agent_id: "finance";
  response_utf8_b64: string;
}

export interface CompatibilityArtifactEvidenceV1 {
  openclaw: ArtifactHashResultV1;
  plugin: ArtifactHashResultV1;
  core: CoreDistributionEvidenceV1;
  finance_commit: string;
  runtime_commit: string;
}

function sha256(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, fields: readonly string[], label: string): void {
  if (Object.keys(value).sort().join(",") !== [...fields].sort().join(",")) {
    throw new Error(`${label} fields are not exact.`);
  }
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return true;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) return true;
  }
  return false;
}

function parseFixtureSet(value: unknown): CompatibilityFixtureSetV1 {
  const fixtureSet = object(value, "compatibility fixture set");
  exact(fixtureSet, ["version", "cases"], "compatibility fixture set");
  if (fixtureSet.version !== "finance-ai-model-compatibility-fixtures-v6" ||
      !Array.isArray(fixtureSet.cases) || fixtureSet.cases.length !== 4) {
    throw new Error("Compatibility fixture set identity is invalid.");
  }
  const cases = fixtureSet.cases.map((candidate, index) => {
    const item = object(candidate, "compatibility case");
    const caseId = FIXTURE_CASE_IDS[index];
    exact(item, [
      "case_id", "source_kind", "catalog", "parent_payload", "ocr_layout", "expected",
      "forbidden_output_fragments",
    ], "compatibility case");
    if (caseId === undefined || item.case_id !== caseId ||
        (item.source_kind !== "telegram_text" &&
         item.source_kind !== "receipt_local_ocr_text") ||
        !Array.isArray(item.forbidden_output_fragments) ||
        item.forbidden_output_fragments.some((entry) => typeof entry !== "string")) {
      throw new Error("Compatibility case identity is invalid.");
    }
    const catalog = object(item.catalog, "compatibility catalog");
    let sourceBytes = 0;
    if (Object.keys(catalog).length === 0 || Object.keys(catalog).length > MAX_CATALOG_ENTRIES ||
        Object.entries(catalog).some(([ref, text]) => (
          !/^[a-z][0-9]{4}$/u.test(ref) || typeof text !== "string" || text.length === 0 ||
          hasUnpairedSurrogate(text) || /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(text) ||
          Buffer.byteLength(text, "utf8") > MAX_SEGMENT_BYTES ||
          (sourceBytes += Buffer.byteLength(text, "utf8")) > MAX_SOURCE_BYTES
        ))) {
      throw new Error("Compatibility case catalog is invalid.");
    }
    return {
      case_id: caseId,
      source_kind: item.source_kind,
      catalog: catalog as Record<string, string>,
      parent_payload: object(item.parent_payload, "compatibility parent payload"),
      ocr_layout: item.ocr_layout === null ? null : object(item.ocr_layout, "OCR layout"),
      expected: object(item.expected, "compatibility expected result"),
      forbidden_output_fragments: [...item.forbidden_output_fragments] as string[],
    } satisfies CompatibilityCaseV1;
  });
  return { version: "finance-ai-model-compatibility-fixtures-v6", cases };
}

async function loadBoundAsset(
  coreDistributionRoot: string,
  entryValue: unknown,
  expectedPath: string,
  expectedVersion: string,
  expectedSha256: string,
): Promise<Buffer> {
  const entry = object(entryValue, "asset registry entry");
  exact(entry, ["path", "version", "sha256", "byte_count"], "asset registry entry");
  if (entry.path !== expectedPath || entry.version !== expectedVersion ||
      entry.sha256 !== expectedSha256 ||
      typeof entry.byte_count !== "number" || !Number.isSafeInteger(entry.byte_count)) {
    throw new Error("Compatibility asset binding is invalid.");
  }
  const body = await readFile(join(coreDistributionRoot, expectedPath));
  if (body.byteLength !== entry.byte_count || sha256(body) !== entry.sha256) {
    throw new Error("Compatibility asset hash or byte count does not verify.");
  }
  return body;
}

export async function loadCompatibilityAssetsV1(
  coreDistributionRootValue: string,
): Promise<CompatibilityAssetsV1> {
  const coreDistributionRoot = resolve(coreDistributionRootValue);
  if (coreDistributionRoot !== coreDistributionRootValue ||
      await realpath(coreDistributionRoot) !== coreDistributionRoot) {
    throw new Error("Core distribution root must be an absolute real path.");
  }
  const registryBody = await readFile(join(
    coreDistributionRoot,
    "finance_core/resources/finance_ai/asset_registry_v2.json",
  ));
  const registry = object(
    JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(registryBody)),
    "asset registry",
  );
  exact(registry, ["schema_version", "registry_version", "assets"], "asset registry");
  if (registry.schema_version !== "finance-ai-asset-registry-v2" ||
      registry.registry_version !== "finance-ai-asset-registry-v2") {
    throw new Error("Compatibility asset registry identity is invalid.");
  }
  const entries = object(registry.assets, "asset registry assets");
  exact(entries, ["agent_projection_policy", "model_compatibility_fixtures", "prompt"],
    "asset registry assets");
  const prompt = await loadBoundAsset(
    coreDistributionRoot,
    entries.prompt,
    "finance_core/resources/finance_ai/prompt_v1.txt",
    "finance-ai-prompt-v5",
    PROMPT_SHA256,
  );
  const fixtures = await loadBoundAsset(
    coreDistributionRoot,
    entries.model_compatibility_fixtures,
    "finance_core/resources/finance_ai/model_compatibility_fixture_set_v1.json",
    "finance-ai-model-compatibility-fixtures-v6",
    FIXTURE_SHA256,
  );
  return {
    prompt: new TextDecoder("utf-8", { fatal: true }).decode(prompt),
    fixtures: parseFixtureSet(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(fixtures))),
    identity_sha256: sha256(Buffer.from(
      `finance-compatibility-assets-v1\0${sha256(registryBody)}\0${sha256(prompt)}\0${sha256(fixtures)}`,
      "utf8",
    )),
  };
}

function fixtureMessage(caseValue: CompatibilityCaseV1): string {
  const kind = caseValue.source_kind === "telegram_text" ? "raw_text" : "ocr_block";
  const message = JSON.stringify({
    projection_schema_version: "finance-ai-input-v1",
    source_kind: caseValue.source_kind === "telegram_text"
      ? "telegram_raw_text"
      : "receipt_local_ocr_text",
    evidence_catalog: Object.entries(caseValue.catalog).map(([ref, text]) => ({ ref, kind, text })),
    fallback_reasons: ["compatibility_evaluation"],
  });
  if (Buffer.byteLength(message, "utf8") > MAX_REQUEST_BYTES) {
    throw new Error("Compatibility request exceeds the bounded policy.");
  }
  return message;
}

function requireAttribution(
  result: CompatibilityLlmResultV1,
  projection: FinanceAgentConfigProjectionV2,
): void {
  if (result.provider !== projection.canonical_provider ||
      result.model !== projection.canonical_model || result.agentId !== AGENT_ID ||
      result.audit?.caller?.kind !== "plugin" || result.audit.caller.id !== "finance-bridge" ||
      result.audit.caller.name !== undefined || result.audit.purpose !== PURPOSE ||
      result.audit.sessionKey !== undefined) {
    throw new Error("Compatibility completion attribution does not match the pinned projection.");
  }
}

export async function runCompatibilityHarnessV1(params: {
  runtime: CompatibilityLlmRuntimeV1;
  runner: BridgeRunner;
  projection: FinanceAgentConfigProjectionV2;
  assets: CompatibilityAssetsV1;
  now?: () => number;
  deadlineMs?: number;
}): Promise<CompatibilityHarnessOutcomeV1[]> {
  if (params.runtime.capabilities?.maxRetries !== true) {
    throw createOperatorFailureV1(
      "Pinned OpenClaw host does not prove effective maxRetries control.",
      {
        category: "OPERATOR_REFUSED",
        phase: "preflight",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  const now = params.now ?? (() => performance.now());
  const deadlineMs = params.deadlineMs ?? CASE_DEADLINE_MS;
  if (!Number.isSafeInteger(deadlineMs) || deadlineMs <= 0 || deadlineMs > CASE_DEADLINE_MS) {
    throw createOperatorFailureV1(
      "Compatibility deadline is invalid.",
      {
        category: "OPERATOR_REFUSED",
        phase: "preflight",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  const outcomes: CompatibilityHarnessOutcomeV1[] = [];
  if (Buffer.byteLength(params.assets.prompt, "utf8") > MAX_REQUEST_BYTES) {
    throw createOperatorFailureV1(
      "Compatibility prompt exceeds the bounded policy.",
      {
        category: "OPERATOR_REFUSED",
        phase: "preflight",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  for (const fixture of params.assets.fixtures.cases) {
    const controller = new AbortController();
    const startedAt = now();
    let timeoutTriggered = false;
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        timeoutTriggered = true;
        controller.abort();
        reject(new Error("Compatibility completion deadline exceeded."));
      }, deadlineMs);
    });
    let result: CompatibilityLlmResultV1;
    try {
      const content = fixtureMessage(fixture);
      if (Buffer.byteLength(params.assets.prompt, "utf8") +
          Buffer.byteLength(content, "utf8") > MAX_REQUEST_BYTES) {
        throw new Error("Compatibility request exceeds the bounded policy.");
      }
      const request = {
        messages: [{ role: "user" as const, content }],
        model: `${params.projection.canonical_provider}/${params.projection.canonical_model}`,
        maxTokens: 1024,
        temperature: 0 as const,
        systemPrompt: params.assets.prompt,
        purpose: PURPOSE,
        agentId: AGENT_ID as "finance",
        maxRetries: 0 as const,
        signal: controller.signal,
      };
      result = await Promise.race([params.runtime.complete(request), timeout]);
    } catch {
      const observedElapsedMs = Math.floor(now() - startedAt);
      const elapsedMs = Number.isSafeInteger(observedElapsedMs) && observedElapsedMs >= 0
        ? observedElapsedMs
        : deadlineMs;
      const deadlineExceeded = timeoutTriggered || elapsedMs >= deadlineMs;
      if (deadlineExceeded) controller.abort();
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} did not settle successfully; stop without retry.`,
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "compatibility_case",
          timer_layer: deadlineExceeded ? "compatibility_case" : "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: deadlineExceeded,
        },
      );
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
    const elapsedMs = Math.floor(now() - startedAt);
    if (!Number.isSafeInteger(elapsedMs) || elapsedMs < 0 || elapsedMs >= deadlineMs) {
      controller.abort();
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} exceeded its fixed deadline.`,
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "compatibility_case",
          timer_layer: "compatibility_case",
          elapsed_ms: Number.isSafeInteger(elapsedMs) && elapsedMs >= 0 ? elapsedMs : deadlineMs,
          timeout_triggered: true,
        },
      );
    }
    try {
      requireAttribution(result, params.projection);
    } catch {
      throw createOperatorFailureV1(
        "Compatibility completion attribution does not match the pinned projection.",
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "compatibility_case",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
        },
      );
    }
    if (typeof result.text !== "string" || result.text.length > MAX_RESPONSE_BYTES ||
        hasUnpairedSurrogate(result.text)) {
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} returned invalid UTF-16 text.`,
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "compatibility_case",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
        },
      );
    }
    const response = Buffer.from(result.text, "utf8");
    if (response.byteLength > MAX_RESPONSE_BYTES) {
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} returned an oversized response.`,
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "compatibility_case",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
        },
      );
    }
    outcomes.push({
      case_id: fixture.case_id,
      ordinary_agent_turn_count: 0,
      isolated_completion_count: 1,
      provider_dispatch_count: 1,
      effective_max_retries: 0,
      elapsed_ms: elapsedMs,
      observed_provider: result.provider,
      observed_model: result.model,
      observed_agent_id: AGENT_ID,
      response_utf8_b64: response.toString("base64"),
    });
    const outcome = outcomes[outcomes.length - 1]!;
    let verification: BridgeResponse;
    try {
      verification = await params.runner.run(createBridgeRequest(
        "verify_ai_model_compatibility_case_v2",
        {
          config_projection: params.projection as unknown as JsonObject,
          harness_outcome: outcome as unknown as JsonObject,
        },
      ), 30_000);
    } catch (error) {
      const category = bridgeProcessFailureCategory(error) ?? "BRIDGE_UNAVAILABLE";
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} Python verification was unavailable: ${category}; ` +
          "stop without retry.",
        {
          category: "BRIDGE_REFUSED",
          phase: "python_verification",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
        },
      );
    }
    if (verification.status !== "ok") {
      const category: OperatorFailureCategoryV1 = verification.error.code ===
        "AI_MODEL_EVAL_REFUSED" ? "AI_MODEL_EVAL_REFUSED" : "COMPATIBILITY_REFUSED";
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} was refused by Python: ${verification.error.code}`,
        {
          category,
          phase: "python_verification",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
          ...safeVerificationDetails(verification),
        },
      );
    }
    exact(verification.result as Record<string, unknown>, [
      "case_id", "verified", "response_sha256",
    ], "compatibility case verification result");
    if (verification.result.case_id !== fixture.case_id ||
        verification.result.verified !== true ||
        verification.result.response_sha256 !== sha256(response)) {
      throw createOperatorFailureV1(
        `Compatibility case ${fixture.case_id} verification result is invalid.`,
        {
          category: "COMPATIBILITY_REFUSED",
          phase: "python_verification",
          timer_layer: "none",
          elapsed_ms: elapsedMs,
          timeout_triggered: false,
        },
      );
    }
  }
  return outcomes;
}

export async function verifyArtifactEvidenceV1(params: {
  config: FinanceBridgeConfig;
  openclawArtifactRoot: string;
  loadedPluginRoot: string;
  loadedPluginSource: string;
  expectedOpenclawVersion: string;
}): Promise<CompatibilityArtifactEvidenceV1> {
  const loadedPluginRoot = resolve(params.loadedPluginRoot);
  const expectedPluginSource = join(loadedPluginRoot, "dist/src/index.js");
  const overlaps = (left: string, right: string): boolean => {
    const child = relative(left, right);
    return child === "" || (!child.startsWith("..") && !isAbsolute(child));
  };
  if (loadedPluginRoot !== params.loadedPluginRoot ||
      await realpath(params.loadedPluginRoot) !== loadedPluginRoot ||
      overlaps(params.config.repoRoot, loadedPluginRoot) ||
      overlaps(loadedPluginRoot, params.config.repoRoot) ||
      overlaps(params.config.coreDistributionRoot, loadedPluginRoot) ||
      overlaps(loadedPluginRoot, params.config.coreDistributionRoot) ||
      resolve(params.loadedPluginSource) !== expectedPluginSource ||
      await realpath(params.loadedPluginSource) !== expectedPluginSource) {
    throw new Error("Loaded plugin source must be the actual Finance build entry.");
  }
  await revalidatePythonExecutableForSpawn(params.config);
  const [openclaw, plugin, core, head, statusResult] = await Promise.all([
    computeArtifactHashV1("openclaw_package", params.openclawArtifactRoot),
    computeArtifactHashV1("finance_plugin_build", params.loadedPluginRoot),
    verifyCoreDistributionV1(params.config),
    execFile("git", ["rev-parse", "--verify", "HEAD"], { cwd: params.config.repoRoot }),
    execFile("git", ["status", "--porcelain=v1", "--untracked-files=all"], {
      cwd: params.config.repoRoot,
    }),
  ]);
  const runtimeCommit = head.stdout.trim();
  if (!/^[0-9a-f]{40}$/u.test(runtimeCommit) || statusResult.stdout.trim() !== "") {
    throw new Error("Finance runtime repository must be clean and at one exact commit.");
  }
  if (openclaw.package_version !== params.expectedOpenclawVersion ||
      openclaw.artifact_sha256 !== params.config.agentProfileV2.openclawPackageSha256 ||
      plugin.artifact_sha256 !== params.config.agentProfileV2.pluginBuildSha256) {
    throw new Error("Configured Agent profile hashes do not match the exact runtime artifacts.");
  }
  return {
    openclaw,
    plugin,
    core,
    finance_commit: core.core_commit,
    runtime_commit: runtimeCommit,
  };
}

export async function registerCompatibilityReceiptV1(params: {
  runner: BridgeRunner;
  workspaceRoot: string;
  projection: FinanceAgentConfigProjectionV2;
  outcomes: CompatibilityHarnessOutcomeV1[];
}): Promise<JsonObject> {
  const projectionHash = canonicalProjectionSha256V2(params.projection);
  let response: BridgeResponse;
  try {
    response = await params.runner.run(createBridgeRequest(
      "register_ai_model_compatibility_receipt_v2",
      {
        workspace_path: params.workspaceRoot,
        config_projection: params.projection as unknown as JsonObject,
        harness_outcomes: params.outcomes as unknown as JsonValue[],
      },
      `bridge-register-ai-model-receipt-v2:${projectionHash}`,
    ), 30_000);
  } catch (error) {
    const category = bridgeProcessFailureCategory(error) ?? "BRIDGE_UNAVAILABLE";
    throw createOperatorFailureV1(
      `Compatibility receipt registration refused: ${category}`,
      {
        category: "BRIDGE_REFUSED",
        phase: "receipt_registration",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  if (response.status !== "ok") {
    throw createOperatorFailureV1(
      `Compatibility receipt registration refused: ${response.error.code}`,
      {
        category: "COMPATIBILITY_REFUSED",
        phase: "receipt_registration",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  exact(response.result as Record<string, unknown>, [
    "receipt_public_id", "receipt_material_hash", "config_projection_hash",
    "canonical_model", "idempotent_replay",
  ], "compatibility receipt result");
  if (response.result.config_projection_hash !== projectionHash) {
    throw createOperatorFailureV1(
      "Compatibility receipt projection hash does not match.",
      {
        category: "COMPATIBILITY_REFUSED",
        phase: "receipt_registration",
        timer_layer: "none",
        elapsed_ms: 0,
        timeout_triggered: false,
      },
    );
  }
  return response.result;
}
