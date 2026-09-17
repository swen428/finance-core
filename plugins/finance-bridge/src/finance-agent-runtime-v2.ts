import { createHash } from "node:crypto";
import { isAbsolute, join, resolve } from "node:path";

import {
  projectFinanceAgentConfigV2,
  type FinanceAgentConfigProjectionV2,
} from "./agent-profile-projection-v2.js";
import type { FinanceAgentProfileEvidenceV2 } from "./config.js";
import { TOOL_NAMES } from "./tools.js";

const DISPLAY_ALIAS = /^[^\p{C}\p{Zl}\p{Zp}]{1,64}$/u;
const VALID_OPENCLAW_AGENT_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/iu;
const INVALID_OPENCLAW_AGENT_ID_CHARS = /[^a-z0-9_-]+/giu;
const CODEX_THREAD_TOOL = "codex_threads";

export interface FinanceAgentConfigRefusalV2 {
  schema_version: "finance-openclaw-agent-config-refusal-v2";
  refusal_code: "projection_invalid";
  evidence_sha256: string;
}

export interface LoadedPluginSourceEvidenceV2 {
  pluginId: string;
  packageName: string;
  source: string;
  rootDir: string;
  origin: string;
  status: string;
  providerIds: string[];
  sourceConfigEntrySha256: string;
}

function getLoadedPluginSourceV2(runtimeValue: unknown, pluginId: string): unknown {
  if (typeof runtimeValue !== "object" || runtimeValue === null) {
    throw new Error("Pinned OpenClaw host does not expose loaded plugin source evidence.");
  }
  const runtime = runtimeValue as {
    pluginSources?: { getLoaded?: (pluginId: string) => unknown };
  };
  if (typeof runtime.pluginSources?.getLoaded !== "function") {
    throw new Error("Pinned OpenClaw host does not expose loaded plugin source evidence.");
  }
  return runtime.pluginSources.getLoaded(pluginId);
}

export function getLoadedCodexPluginSourceV2(runtimeValue: unknown): unknown {
  return getLoadedPluginSourceV2(runtimeValue, "codex");
}

export function getLoadedOpenAIPluginSourceV2(runtimeValue: unknown): unknown {
  return getLoadedPluginSourceV2(runtimeValue, "openai");
}

export interface ReviewedFinanceHostRuntimeV2 {
  hostConfig: Record<string, unknown>;
  codexPluginSource: Record<string, unknown>;
  openaiPluginSource: Record<string, unknown>;
}

export function resolveReviewedFinanceHostRuntimeV2(
  runtimeValue: unknown,
): ReviewedFinanceHostRuntimeV2 {
  const runtime = record(runtimeValue, "OpenClaw runtime");
  const configRuntime = record(runtime.config, "OpenClaw runtime config API") as {
    current?: () => unknown;
  };
  if (typeof configRuntime.current !== "function") {
    throw new Error("Pinned OpenClaw host does not expose the current config snapshot.");
  }
  const current = structuredClone(record(configRuntime.current(), "OpenClaw runtime config"));
  const codexSource = record(
    getLoadedCodexPluginSourceV2(runtime),
    "loaded Codex plugin source",
  );
  const openaiSource = record(
    getLoadedOpenAIPluginSourceV2(runtime),
    "loaded OpenAI plugin source",
  );
  if (codexSource.sourceConfigEntrySha256 !== pluginSourceEntrySha256({ enabled: true })) {
    throw new Error("Source Codex plugin entry is not exactly enabled-only.");
  }
  if (openaiSource.sourceConfigEntrySha256 !== pluginSourceEntrySha256({ enabled: true })) {
    throw new Error("Source OpenAI plugin entry is not exactly enabled-only.");
  }

  const currentPlugins = record(current.plugins, "runtime plugins");
  const currentEntries = record(currentPlugins.entries, "runtime plugins.entries");
  const currentCodexEntry = record(currentEntries.codex, "runtime Codex plugin entry");
  exact(currentCodexEntry, ["enabled", "config"], "runtime Codex plugin entry");
  if (currentCodexEntry.enabled !== true) throw new Error("Runtime Codex plugin must be enabled.");
  const currentCodexConfig = record(currentCodexEntry.config, "runtime Codex plugin config");
  exact(
    currentCodexConfig,
    ["codexDynamicToolsLoading", "codexDynamicToolsExclude"],
    "runtime Codex plugin config",
  );
  if (currentCodexConfig.codexDynamicToolsLoading !== "searchable" ||
      !Array.isArray(currentCodexConfig.codexDynamicToolsExclude) ||
      currentCodexConfig.codexDynamicToolsExclude.length !== 0) {
    throw new Error("Runtime Codex plugin defaults do not match the reviewed host artifact.");
  }
  const currentOpenAiEntry = record(currentEntries.openai, "runtime OpenAI plugin entry");
  exact(currentOpenAiEntry, ["enabled", "config"], "runtime OpenAI plugin entry");
  if (currentOpenAiEntry.enabled !== true) {
    throw new Error("Runtime OpenAI plugin must be enabled.");
  }
  const currentOpenAiConfig = record(
    currentOpenAiEntry.config,
    "runtime OpenAI plugin config",
  );
  exact(currentOpenAiConfig, ["personality"], "runtime OpenAI plugin config");
  if (currentOpenAiConfig.personality !== "friendly") {
    throw new Error("Runtime OpenAI plugin defaults do not match the reviewed host artifact.");
  }

  currentEntries.codex = { enabled: true };
  currentEntries.openai = { enabled: true };
  return {
    hostConfig: current,
    codexPluginSource: structuredClone(codexSource),
    openaiPluginSource: structuredClone(openaiSource),
  };
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be a string array.`);
  }
  return [...value];
}

function exact(value: Record<string, unknown>, keys: string[], label: string): void {
  if (Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) {
    throw new Error(`${label} fields are not exact.`);
  }
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (typeof value !== "object" || value === null) return value;
  const source = value as Record<string, unknown>;
  return Object.fromEntries(Object.keys(source).sort().map((key) => [key, canonical(source[key])]));
}

function hash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(canonical(value)), "utf8").digest("hex");
}

function pluginSourceEntrySha256(value: unknown): string {
  return createHash("sha256")
    .update("openclaw-plugin-source-entry-v1\0", "utf8")
    .update(JSON.stringify(canonical(value)), "utf8")
    .digest("hex");
}

function requireFinanceToolsDenied(value: unknown, label: string): { deny: string[] } {
  const policy = record(value, label);
  exact(policy, ["deny"], label);
  const denied = stringArray(policy.deny, `${label}.deny`);
  if (denied.includes("*") || TOOL_NAMES.some((name) => !denied.includes(name)) ||
      !denied.includes(CODEX_THREAD_TOOL)) {
    throw new Error(`${label} must explicitly deny every Finance tool and codex_threads.`);
  }
  return { deny: [...new Set(denied)].sort() };
}

function reviewedPluginLoadPaths(plugins: Record<string, unknown>): string[] {
  const load = record(plugins.load, "plugins.load");
  exact(load, ["paths"], "plugins.load");
  const paths = stringArray(load.paths, "plugins.load.paths");
  if (paths.length !== 2 || new Set(paths).size !== 2 ||
      paths.some((path) => !isAbsolute(path) || resolve(path) !== path)) {
    throw new Error("plugins.load.paths must be two distinct normalized absolute paths.");
  }
  return paths;
}

function reviewedCodexPluginSource(
  value: unknown,
  expectedRoot: string,
): LoadedPluginSourceEvidenceV2 {
  const source = record(value, "loaded Codex plugin source");
  exact(source, [
    "pluginId", "packageName", "source", "rootDir", "origin", "status", "providerIds",
    "sourceConfigEntrySha256",
  ], "loaded Codex plugin source");
  const providerIds = stringArray(source.providerIds, "loaded Codex provider ids");
  const expectedSource = join(expectedRoot, "dist", "index.js");
  if (source.pluginId !== "codex" || source.packageName !== "@openclaw/codex" ||
      source.rootDir !== expectedRoot || source.source !== expectedSource ||
      source.origin !== "config" || source.status !== "loaded" ||
      source.sourceConfigEntrySha256 !== pluginSourceEntrySha256({ enabled: true }) ||
      providerIds.length !== 1 || providerIds[0] !== "codex") {
    throw new Error("The loaded Codex provider is not the reviewed config-selected source.");
  }
  return {
    pluginId: "codex",
    packageName: "@openclaw/codex",
    source: expectedSource,
    rootDir: expectedRoot,
    origin: "config",
    status: "loaded",
    providerIds: ["codex"],
    sourceConfigEntrySha256: pluginSourceEntrySha256({ enabled: true }),
  };
}

function reviewedOpenAiPluginSource(
  value: unknown,
  openclawArtifactRoot: string,
): LoadedPluginSourceEvidenceV2 {
  const source = record(value, "loaded OpenAI plugin source");
  exact(source, [
    "pluginId", "packageName", "source", "rootDir", "origin", "status", "providerIds",
    "sourceConfigEntrySha256",
  ], "loaded OpenAI plugin source");
  const providerIds = stringArray(source.providerIds, "loaded OpenAI provider ids");
  const expectedRoot = join(openclawArtifactRoot, "dist", "extensions", "openai");
  const expectedSource = join(expectedRoot, "index.js");
  if (source.pluginId !== "openai" || source.packageName !== "@openclaw/openai-provider" ||
      source.rootDir !== expectedRoot ||
      source.source !== expectedSource || source.origin !== "bundled" ||
      source.status !== "loaded" ||
      source.sourceConfigEntrySha256 !== pluginSourceEntrySha256({ enabled: true }) ||
      providerIds.length !== 1 || providerIds[0] !== "openai") {
    throw new Error("The loaded OpenAI provider is not the reviewed bundled source.");
  }
  return {
    pluginId: "openai",
    packageName: "@openclaw/openai-provider",
    source: expectedSource,
    rootDir: expectedRoot,
    origin: "bundled",
    status: "loaded",
    providerIds: ["openai"],
    sourceConfigEntrySha256: pluginSourceEntrySha256({ enabled: true }),
  };
}

function rejectChannelOverrides(value: unknown): void {
  if (value === undefined) return;
  const pending: unknown[] = [record(value, "channels")];
  while (pending.length > 0) {
    const current = pending.pop();
    if (typeof current !== "object" || current === null || Array.isArray(current)) continue;
    const object = current as Record<string, unknown>;
    if (object.tools !== undefined || object.toolsBySender !== undefined) {
      throw new Error("Channel-level Finance tool overrides are forbidden.");
    }
    if (object.agentId !== undefined &&
        normalizeOpenClawAgentId(object.agentId, "channel Agent id") === "finance") {
      throw new Error("Channel-level routes must not target finance.");
    }
    pending.push(...Object.values(object));
  }
}

function normalizeOpenClawAgentId(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${label} must be a non-empty string.`);
  }
  const trimmed = value.trim();
  const normalized = trimmed.toLowerCase();
  if (VALID_OPENCLAW_AGENT_ID.test(trimmed)) return normalized;
  return normalized
    .replace(INVALID_OPENCLAW_AGENT_ID_CHARS, "-")
    .replace(/^-+/u, "")
    .replace(/-+$/u, "")
    .slice(0, 64) || "main";
}

function rejectFinanceSubagentTargets(value: unknown, label: string): void {
  if (value === undefined) return;
  const subagents = record(value, label);
  if (subagents.allowAgents === undefined) return;
  const allowed = stringArray(subagents.allowAgents, `${label}.allowAgents`);
  if (allowed.some((agentId) => (
    agentId.trim() === "*" ||
    normalizeOpenClawAgentId(agentId, `${label}.allowAgents entry`) === "finance"
  ))) {
    throw new Error(`${label} must not admit finance as a subagent target.`);
  }
}

function rejectFinanceBindings(value: unknown): void {
  const bindings = value === undefined ? [] : value;
  if (!Array.isArray(bindings)) throw new Error("bindings must be an array.");
  for (const candidate of bindings) {
    const binding = record(candidate, "ordinary Agent route binding");
    if (normalizeOpenClawAgentId(binding.agentId, "binding.agentId") === "finance") {
      throw new Error("finance Agent must not have an ordinary Agent route binding.");
    }
  }
}

function rejectFinanceWebhookRoutes(value: unknown): void {
  const hooks = record(value, "hooks");
  if (hooks.enabled !== false) {
    throw new Error("OpenClaw webhook Agent routes must be explicitly disabled.");
  }
  if (hooks.allowedAgentIds !== undefined) {
    const allowed = stringArray(hooks.allowedAgentIds, "hooks.allowedAgentIds");
    if (allowed.includes("*") || allowed.some(
      (agentId) => normalizeOpenClawAgentId(agentId, "hooks.allowedAgentIds entry") === "finance",
    )) {
      throw new Error("OpenClaw webhook Agent routes must not admit finance.");
    }
  }
  if (hooks.mappings === undefined) return;
  if (!Array.isArray(hooks.mappings)) throw new Error("hooks.mappings must be an array.");
  for (const candidate of hooks.mappings) {
    const mapping = record(candidate, "hooks mapping");
    if (mapping.agentId !== undefined &&
        normalizeOpenClawAgentId(mapping.agentId, "hooks mapping agentId") === "finance") {
      throw new Error("OpenClaw webhook mapping must not target finance.");
    }
    if (typeof mapping.sessionKey === "string" &&
        mapping.sessionKey.trim().toLowerCase().startsWith("agent:finance:")) {
      throw new Error("OpenClaw webhook session must not target finance.");
    }
  }
}

function extractFinanceAgent(hostConfigValue: unknown): {
  agent: Record<string, unknown>;
  primary: string;
  alias: string;
  rootTools: { deny: string[] };
  financeTools: { deny: string[] };
  pluginLoadPaths: string[];
  pluginBinding: Record<string, unknown>;
  memoryPolicy: Record<string, unknown>;
} {
  const host = record(hostConfigValue, "OpenClaw config");
  const plugins = record(host.plugins, "plugins");
  const allowed = stringArray(plugins.allow, "plugins.allow");
  if (allowed.length !== 4 || allowed[0] !== "telegram" ||
      allowed[1] !== "finance-bridge" || allowed[2] !== "codex" ||
      allowed[3] !== "openai") {
    throw new Error(
      "plugins.allow must be the reviewed Telegram, Finance, Codex, and OpenAI list.",
    );
  }
  const pluginLoadPaths = reviewedPluginLoadPaths(plugins);
  const entries = record(plugins.entries, "plugins.entries");
  const codexEntry = record(entries.codex, "Codex plugin entry");
  exact(codexEntry, ["enabled"], "Codex plugin entry");
  if (codexEntry.enabled !== true) throw new Error("Codex plugin must be enabled.");
  const openaiEntry = record(entries.openai, "OpenAI plugin entry");
  exact(openaiEntry, ["enabled"], "OpenAI plugin entry");
  if (openaiEntry.enabled !== true) throw new Error("OpenAI plugin must be enabled.");
  const financeEntry = record(entries["finance-bridge"], "finance plugin entry");
  if (financeEntry.enabled !== true) throw new Error("Finance plugin must be enabled.");
  const llm = record(financeEntry.llm, "finance plugin LLM policy");
  exact(llm, ["allowModelOverride", "allowAgentIdOverride"], "finance plugin LLM policy");
  if (llm.allowModelOverride !== true || llm.allowAgentIdOverride !== true) {
    throw new Error("Finance plugin LLM overrides must be enabled without a model list.");
  }

  const rootTools = requireFinanceToolsDenied(host.tools, "root tools policy");
  rejectChannelOverrides(host.channels);
  const agents = record(host.agents, "agents");
  if (agents.defaults !== undefined) {
    const defaults = record(agents.defaults, "agents.defaults");
    if (defaults.heartbeat !== undefined) {
      throw new Error("Agent defaults heartbeat must not schedule finance.");
    }
    rejectFinanceSubagentTargets(defaults.subagents, "agents.defaults.subagents");
    if (defaults.tools !== undefined) {
      const defaultsTools = record(defaults.tools, "agents.defaults.tools");
      if (defaultsTools.allow !== undefined || defaultsTools.alsoAllow !== undefined ||
          defaultsTools.byProvider !== undefined || defaultsTools.toolsBySender !== undefined ||
          (Array.isArray(defaultsTools.deny) && defaultsTools.deny.includes("*"))) {
        throw new Error("Agent defaults must not override the Finance tool denial.");
      }
    }
  }
  if (!Array.isArray(agents.list)) throw new Error("agents.list must be an array.");
  const configuredAgents = agents.list.map((candidate) => record(candidate, "configured Agent"));
  const financeAgents = configuredAgents.filter(
    (candidate) => normalizeOpenClawAgentId(candidate.id, "configured Agent id") === "finance",
  );
  if (financeAgents.length !== 1) throw new Error("Exactly one finance Agent is required.");
  const agent = financeAgents[0]!;
  if (agent.id !== "finance") {
    throw new Error("finance Agent id must use the canonical lowercase spelling.");
  }
  const defaultAgents = configuredAgents.filter((candidate) => candidate.default === true);
  if (defaultAgents.length !== 1 ||
      normalizeOpenClawAgentId(defaultAgents[0]!.id, "default Agent id") === "finance") {
    throw new Error("Exactly one explicit non-finance default Agent is required.");
  }
  for (const configured of configuredAgents) {
    const configuredId = normalizeOpenClawAgentId(configured.id, "configured Agent id");
    requireFinanceToolsDenied(configured.tools, `Agent ${String(configured.id)} tools policy`);
    if (configuredId === "finance" && configured.heartbeat !== undefined) {
      throw new Error("finance Agent heartbeat must remain disabled.");
    }
    rejectFinanceSubagentTargets(
      configured.subagents,
      `Agent ${String(configured.id)} subagents policy`,
    );
  }
  const financeTools = requireFinanceToolsDenied(agent.tools, "finance Agent tools policy");
  if (!financeTools.deny.includes("sessions_spawn")) {
    throw new Error("finance Agent must explicitly deny sessions_spawn.");
  }
  const model = record(agent.model, "finance Agent model");
  exact(model, ["primary", "fallbacks"], "finance Agent model");
  if (typeof model.primary !== "string" || !Array.isArray(model.fallbacks) ||
      model.fallbacks.length !== 0) {
    throw new Error("finance Agent must have one primary model and no fallbacks.");
  }
  const modelMetadata = record(agent.models, "finance Agent model metadata");
  exact(modelMetadata, [model.primary], "finance Agent model metadata");
  const primaryMetadata = record(modelMetadata[model.primary], "finance primary model metadata");
  exact(primaryMetadata, ["alias"], "finance primary model metadata");
  if (typeof primaryMetadata.alias !== "string" ||
      !DISPLAY_ALIAS.test(primaryMetadata.alias) ||
      primaryMetadata.alias !== primaryMetadata.alias.trim() ||
      Buffer.byteLength(primaryMetadata.alias, "utf8") > 64) {
    throw new Error("finance primary model alias is required.");
  }

  const memorySearch = record(agent.memorySearch, "finance memorySearch");
  exact(memorySearch, ["enabled", "experimental"], "finance memorySearch");
  const experimental = record(memorySearch.experimental, "finance session memory policy");
  exact(experimental, ["sessionMemory"], "finance session memory policy");
  if (memorySearch.enabled !== false || experimental.sessionMemory !== false ||
      agent.compaction !== undefined || agent.contextInjection !== "never") {
    throw new Error("finance Agent memory and bootstrap injection must be disabled.");
  }
  if (agent.runtime !== undefined) {
    throw new Error("finance Agent runtime routing must remain disabled.");
  }
  rejectFinanceBindings(host.bindings);
  rejectFinanceWebhookRoutes(host.hooks);
  return {
    agent,
    primary: model.primary,
    alias: primaryMetadata.alias,
    rootTools,
    financeTools,
    pluginLoadPaths,
    pluginBinding: {
      pluginsAllow: allowed,
      pluginLoadPaths,
      codexPlugin: { enabled: true },
      openaiPlugin: { enabled: true },
      financePlugin: { enabled: true, llm },
      ordinaryAgentBindings: [],
      financeIsDefaultAgent: false,
      financeHeartbeatRoute: false,
      financeSubagentTargetRoutes: false,
      financeSessionsSpawnDenied: true,
      webhookAgentRoutes: false,
    },
    memoryPolicy: {
      memorySearch: { enabled: false, experimental: { sessionMemory: false } },
      ordinaryAgentRoute: false,
      compactionRouteReachable: false,
      contextInjection: "never",
    },
  };
}

export function deriveFinanceAgentProjectionV2(
  hostConfig: unknown,
  evidence: FinanceAgentProfileEvidenceV2,
  openclawVersion: string,
  loadedCodexPluginSource: unknown,
  loadedOpenAiPluginSource: unknown,
): FinanceAgentConfigProjectionV2 {
  const extracted = extractFinanceAgent(hostConfig);
  const codexPluginSource = reviewedCodexPluginSource(
    loadedCodexPluginSource,
    extracted.pluginLoadPaths[1]!,
  );
  const openaiPluginSource = reviewedOpenAiPluginSource(
    loadedOpenAiPluginSource,
    resolve(extracted.pluginLoadPaths[1]!, "..", "..", ".."),
  );
  return projectFinanceAgentConfigV2({
    openclawVersion,
    openclawPackageSha256: evidence.openclawPackageSha256,
    financeCommit: evidence.financeCommit,
    pluginBuildSha256: evidence.pluginBuildSha256,
    agents: {
      list: [{
        id: "finance",
        displayAlias: extracted.alias,
        executionClass: evidence.executionClass,
        model: { primary: extracted.primary, fallbacks: [] },
      }],
    },
    policies: {
      toolPolicySha256: hash({
        root: extracted.rootTools,
        finance: extracted.financeTools,
      }),
      memoryPolicySha256: hash(extracted.memoryPolicy),
      pluginBindingPolicySha256: hash({
        ...extracted.pluginBinding,
        loadedCodexPluginSource: codexPluginSource,
        loadedOpenAiPluginSource: openaiPluginSource,
      }),
      projectionPolicyVersion: "finance-openclaw-agent-projection-policy-v2",
      projectionPolicySha256: "08ec7d514067f0e250e0d6e6efeafd817f1e5765c322f2709cce31d3fbab1d08",
      promptVersion: "finance-ai-prompt-v5",
      promptSha256: "48f4d53ba802569bfe929b24d9b83e309851f8bfc18e324e8f0f5e2bd09f4644",
      effectiveMaxRetries: 0,
    },
  });
}

export function financeProjectionOrRefusalV2(
  hostConfig: unknown,
  evidence: FinanceAgentProfileEvidenceV2,
  openclawVersion: string,
  loadedCodexPluginSource: unknown,
  loadedOpenAiPluginSource: unknown,
): FinanceAgentConfigProjectionV2 | FinanceAgentConfigRefusalV2 {
  try {
    return deriveFinanceAgentProjectionV2(
      hostConfig,
      evidence,
      openclawVersion,
      loadedCodexPluginSource,
      loadedOpenAiPluginSource,
    );
  } catch {
    return {
      schema_version: "finance-openclaw-agent-config-refusal-v2",
      refusal_code: "projection_invalid",
      evidence_sha256: hash({
        schema_version: "finance-openclaw-agent-config-refusal-evidence-v2",
        openclaw_version: openclawVersion,
        openclaw_package_sha256: evidence.openclawPackageSha256,
        finance_commit: evidence.financeCommit,
        plugin_build_sha256: evidence.pluginBuildSha256,
      }),
    };
  }
}

export function validateFinanceHostPolicyV2(hostConfig: unknown): void {
  extractFinanceAgent(hostConfig);
}

export function validateLoadedFinancePluginRootV2(
  hostConfig: unknown,
  loadedPluginRoot: string,
): void {
  const extracted = extractFinanceAgent(hostConfig);
  if (!isAbsolute(loadedPluginRoot) || resolve(loadedPluginRoot) !== loadedPluginRoot ||
      extracted.pluginLoadPaths[0] !== loadedPluginRoot) {
    throw new Error("The loaded Finance plugin root is not the reviewed config-selected source.");
  }
}

export function validateReviewedPluginSourcesV2(
  hostConfig: unknown,
  loadedFinancePluginRoot: string,
  openclawArtifactRoot: string,
  loadedCodexPluginSource: unknown,
  loadedOpenAiPluginSource: unknown,
): void {
  validateLoadedFinancePluginRootV2(hostConfig, loadedFinancePluginRoot);
  if (!isAbsolute(openclawArtifactRoot) || resolve(openclawArtifactRoot) !== openclawArtifactRoot) {
    throw new Error("OpenClaw artifact root must be a normalized absolute path.");
  }
  const extracted = extractFinanceAgent(hostConfig);
  const reviewedCodexPluginRoot = join(
    openclawArtifactRoot,
    "node_modules",
    "@openclaw",
    "codex",
  );
  if (extracted.pluginLoadPaths[1] !== reviewedCodexPluginRoot) {
    throw new Error("The reviewed Codex plugin root must be inside the hashed OpenClaw artifact closure.");
  }
  reviewedCodexPluginSource(loadedCodexPluginSource, reviewedCodexPluginRoot);
  reviewedOpenAiPluginSource(loadedOpenAiPluginSource, openclawArtifactRoot);
}
