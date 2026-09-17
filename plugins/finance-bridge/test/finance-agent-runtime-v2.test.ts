import assert from "node:assert/strict";
import test from "node:test";

import type { FinanceAgentProfileEvidenceV2 } from "../src/config.js";
import {
  deriveFinanceAgentProjectionV2,
  financeProjectionOrRefusalV2,
  resolveReviewedFinanceHostRuntimeV2,
  validateFinanceHostPolicyV2,
  validateLoadedFinancePluginRootV2,
  validateReviewedPluginSourcesV2,
} from "../src/finance-agent-runtime-v2.js";
import { TOOL_NAMES } from "../src/tools.js";

const FINANCE_PLUGIN_ROOT = "/repo/plugins/finance-bridge";
const OPENCLAW_ARTIFACT_ROOT = "/openclaw";
const CODEX_PLUGIN_ROOT = "/openclaw/node_modules/@openclaw/codex";
const CODEX_SOURCE = {
  pluginId: "codex",
  packageName: "@openclaw/codex",
  source: `${CODEX_PLUGIN_ROOT}/dist/index.js`,
  rootDir: CODEX_PLUGIN_ROOT,
  origin: "config",
  status: "loaded",
  providerIds: ["codex"],
  sourceConfigEntrySha256: "0462103550920ca01c9a510bdb3f62f69032860014fbaa633957a97b85436ed3",
};
const OPENAI_PLUGIN_ROOT = "/openclaw/dist/extensions/openai";
const OPENAI_SOURCE = {
  pluginId: "openai",
  packageName: "@openclaw/openai-provider",
  source: `${OPENAI_PLUGIN_ROOT}/index.js`,
  rootDir: OPENAI_PLUGIN_ROOT,
  origin: "bundled",
  status: "loaded",
  providerIds: ["openai"],
  sourceConfigEntrySha256: "0462103550920ca01c9a510bdb3f62f69032860014fbaa633957a97b85436ed3",
};

const evidence: FinanceAgentProfileEvidenceV2 = {
  openclawPackageSha256: "1".repeat(64),
  financeCommit: "2".repeat(40),
  coreVersion: "0.1.0",
  coreManifestSha256: "4".repeat(64),
  coreWheelSha256: "5".repeat(64),
  coreApiContractVersion: "finance-core-api-v1",
  coreMigrationLedgerDigest: "6".repeat(64),
  pluginBuildSha256: "3".repeat(64),
  executionClass: "cloud_projection",
};

function hostConfig(
  primary = "openai/gpt-5.6-luna",
  alias = "GPT Luna",
): Record<string, unknown> {
  const deny = [...TOOL_NAMES, "codex_threads"];
  const financeDeny = [...deny, "sessions_spawn"];
  return {
    plugins: {
      allow: ["telegram", "finance-bridge", "codex", "openai"],
      load: { paths: [FINANCE_PLUGIN_ROOT, CODEX_PLUGIN_ROOT] },
      entries: {
        codex: { enabled: true },
        openai: { enabled: true },
        "finance-bridge": {
          enabled: true,
          llm: { allowModelOverride: true, allowAgentIdOverride: true },
        },
      },
    },
    tools: { deny },
    agents: {
      list: [
        {
          id: "main",
          default: true,
          model: { primary: "openai/gpt-5.6-sol" },
          tools: { deny },
        },
        {
          id: "finance",
          model: { primary, fallbacks: [] },
          models: { [primary]: { alias } },
          tools: { deny: financeDeny },
          memorySearch: { enabled: false, experimental: { sessionMemory: false } },
          contextInjection: "never",
        },
      ],
    },
    bindings: [{ agentId: "main", match: { channel: "webchat" } }],
    hooks: { enabled: false },
  };
}

function runtimeHostConfig(): Record<string, unknown> {
  const config = structuredClone(hostConfig());
  const entries = (config.plugins as { entries: Record<string, unknown> }).entries;
  entries.codex = {
    enabled: true,
    config: {
      codexDynamicToolsLoading: "searchable",
      codexDynamicToolsExclude: [],
    },
  };
  entries.openai = {
    enabled: true,
    config: { personality: "friendly" },
  };
  return config;
}

test("projection derives cloud and local model choices only from the finance Agent", () => {
  const cloud = deriveFinanceAgentProjectionV2(
    hostConfig(), evidence, "2026.7.1", CODEX_SOURCE, OPENAI_SOURCE,
  );
  assert.equal(cloud.canonical_provider, "openai");
  assert.equal(cloud.canonical_model, "gpt-5.6-luna");
  assert.equal(cloud.display_alias, "GPT Luna");
  assert.equal(cloud.agent_id, "finance");
  assert.equal(cloud.effective_max_retries, 0);

  const local = deriveFinanceAgentProjectionV2(
    hostConfig("lmstudio/qwen3.8", "Qwen 3.8"),
    { ...evidence, executionClass: "local_model" },
    "2026.7.1",
    CODEX_SOURCE,
    OPENAI_SOURCE,
  );
  assert.equal(local.canonical_provider, "lmstudio");
  assert.equal(local.canonical_model, "qwen3.8");
  assert.equal(local.display_alias, "Qwen 3.8");
  assert.equal(local.execution_class, "local_model");
  assert.notEqual(local.canonical_model, cloud.canonical_model);
});

test("host policy permits unrelated Agents but has no second model allowlist", () => {
  const config = hostConfig();
  assert.doesNotThrow(() => validateFinanceHostPolicyV2(config));
  assert.doesNotThrow(() => validateLoadedFinancePluginRootV2(config, FINANCE_PLUGIN_ROOT));
  assert.doesNotThrow(() => validateReviewedPluginSourcesV2(
    config,
    FINANCE_PLUGIN_ROOT,
    OPENCLAW_ARTIFACT_ROOT,
    CODEX_SOURCE,
    OPENAI_SOURCE,
  ));
  assert.deepEqual((config.plugins as {allow: string[]}).allow, [
    "telegram", "finance-bridge", "codex", "openai",
  ]);
  assert.deepEqual((config.plugins as {load: unknown}).load, {
    paths: [FINANCE_PLUGIN_ROOT, CODEX_PLUGIN_ROOT],
  });
  assert.deepEqual(
    (config.plugins as {entries: Record<string, unknown>}).entries.codex,
    { enabled: true },
  );
  assert.deepEqual(
    (config.plugins as {entries: Record<string, unknown>}).entries.openai,
    { enabled: true },
  );
  const llm = (((config.plugins as {entries: Record<string, unknown>}).entries[
    "finance-bridge"
  ]) as {llm: Record<string, unknown>}).llm;
  assert.deepEqual(Object.keys(llm).sort(), ["allowAgentIdOverride", "allowModelOverride"]);
  assert.equal(JSON.stringify(config).includes("allowedModels"), false);
});

test("runtime host review distinguishes schema defaults from operator-authored Codex config", () => {
  const current = runtimeHostConfig();
  const reviewed = resolveReviewedFinanceHostRuntimeV2({
    config: { current: () => current },
    pluginSources: {
      getLoaded: (pluginId: string) => pluginId === "codex" ? CODEX_SOURCE : OPENAI_SOURCE,
    },
  });
  assert.doesNotThrow(() => validateFinanceHostPolicyV2(reviewed.hostConfig));
  assert.deepEqual(
    ((reviewed.hostConfig.plugins as { entries: Record<string, unknown> }).entries.codex),
    { enabled: true },
  );
  assert.deepEqual(
    ((reviewed.hostConfig.plugins as { entries: Record<string, unknown> }).entries.openai),
    { enabled: true },
  );
  assert.deepEqual(reviewed.codexPluginSource, CODEX_SOURCE);
  assert.deepEqual(reviewed.openaiPluginSource, OPENAI_SOURCE);

  const authoredConfigSource = {
    ...CODEX_SOURCE,
    sourceConfigEntrySha256: "3d1a5ce4ec3ea89fc17954d2cd4c192df5f3076f2fc19f97b7594001d17f837f",
  };
  assert.throws(
    () => resolveReviewedFinanceHostRuntimeV2({
      config: { current: () => current },
      pluginSources: {
        getLoaded: (pluginId: string) => (
          pluginId === "codex" ? authoredConfigSource : OPENAI_SOURCE
        ),
      },
    }),
    /Source Codex plugin entry is not exactly enabled-only/u,
  );

  const authoredOpenAiSource = {
    ...OPENAI_SOURCE,
    sourceConfigEntrySha256: "3d1a5ce4ec3ea89fc17954d2cd4c192df5f3076f2fc19f97b7594001d17f837f",
  };
  assert.throws(
    () => resolveReviewedFinanceHostRuntimeV2({
      config: { current: () => current },
      pluginSources: {
        getLoaded: (pluginId: string) => (
          pluginId === "codex" ? CODEX_SOURCE : authoredOpenAiSource
        ),
      },
    }),
    /Source OpenAI plugin entry is not exactly enabled-only/u,
  );

  const driftedRuntime = structuredClone(current);
  const runtimeCodexConfig = (
    ((driftedRuntime.plugins as { entries: Record<string, unknown> }).entries.codex) as {
      config: Record<string, unknown>;
    }
  ).config;
  runtimeCodexConfig.codexDynamicToolsLoading = "direct";
  assert.throws(
    () => resolveReviewedFinanceHostRuntimeV2({
      config: { current: () => driftedRuntime },
      pluginSources: {
        getLoaded: (pluginId: string) => pluginId === "codex" ? CODEX_SOURCE : OPENAI_SOURCE,
      },
    }),
    /defaults do not match/u,
  );

  for (const candidate of [
    { enabled: true },
    { enabled: true, config: {} },
    { enabled: true, config: { personality: "on" } },
    { enabled: true, config: { personality: "friendly", extra: true } },
    { enabled: true, config: { personality: "friendly" }, extra: true },
  ]) {
    const driftedOpenAiRuntime = structuredClone(current);
    ((driftedOpenAiRuntime.plugins as { entries: Record<string, unknown> }).entries.openai) =
      candidate;
    assert.throws(
      () => resolveReviewedFinanceHostRuntimeV2({
        config: { current: () => driftedOpenAiRuntime },
        pluginSources: {
          getLoaded: (pluginId: string) => (
            pluginId === "codex" ? CODEX_SOURCE : OPENAI_SOURCE
          ),
        },
      }),
      /runtime OpenAI plugin|Runtime OpenAI plugin|OpenAI plugin defaults do not match/u,
    );
  }
});

test("runtime host review refuses a missing loaded OpenAI provider before dispatch", () => {
  assert.throws(
    () => resolveReviewedFinanceHostRuntimeV2({
      config: { current: () => runtimeHostConfig() },
      pluginSources: {
        getLoaded: (pluginId: string) => pluginId === "codex" ? CODEX_SOURCE : undefined,
      },
    }),
    /loaded OpenAI plugin source/u,
  );
});

test("unsafe Agent routes, schedules, fallback, memory, tools, alias, and plugin model lists fail closed", () => {
  const mutate = (edit: (config: Record<string, unknown>) => void): Record<string, unknown> => {
    const config = structuredClone(hostConfig());
    edit(config);
    return config;
  };
  const finance = (config: Record<string, unknown>): Record<string, unknown> =>
    ((config.agents as {list: Array<Record<string, unknown>>}).list[1]!);
  const cases = [
    mutate((config) => {
      (config.plugins as {allow: string[]}).allow = ["telegram", "finance-bridge"];
    }),
    mutate((config) => {
      (config.plugins as {allow: string[]}).allow = [
        "telegram", "finance-bridge", "codex", "openai", "unreviewed",
      ];
    }),
    mutate((config) => {
      (config.plugins as {allow: string[]}).allow = [
        "codex", "telegram", "finance-bridge", "openai",
      ];
    }),
    mutate((config) => {
      delete (config.plugins as Record<string, unknown>).load;
    }),
    mutate((config) => {
      (config.plugins as {load: {paths: string[]}}).load.paths = [FINANCE_PLUGIN_ROOT];
    }),
    mutate((config) => {
      (config.plugins as {load: {paths: string[]}}).load.paths = [
        FINANCE_PLUGIN_ROOT, CODEX_PLUGIN_ROOT, "/unreviewed",
      ];
    }),
    mutate((config) => {
      (config.plugins as {load: {paths: string[]}}).load.paths = [
        FINANCE_PLUGIN_ROOT, FINANCE_PLUGIN_ROOT,
      ];
    }),
    mutate((config) => {
      (config.plugins as {load: {paths: string[]}}).load.paths = [
        "relative/finance-bridge", CODEX_PLUGIN_ROOT,
      ];
    }),
    mutate((config) => {
      delete (config.plugins as {entries: Record<string, unknown>}).entries.codex;
    }),
    mutate((config) => {
      (config.plugins as {entries: Record<string, unknown>}).entries.codex = { enabled: false };
    }),
    mutate((config) => {
      (config.plugins as {entries: Record<string, unknown>}).entries.codex = {
        enabled: true,
        config: { unreviewed: true },
      };
    }),
    mutate((config) => {
      delete (config.plugins as {entries: Record<string, unknown>}).entries.openai;
    }),
    mutate((config) => {
      (config.plugins as {entries: Record<string, unknown>}).entries.openai = {
        enabled: false,
      };
    }),
    mutate((config) => {
      (config.plugins as {entries: Record<string, unknown>}).entries.openai = {
        enabled: true,
        config: {},
      };
    }),
    mutate((config) => {
      (finance(config).model as {fallbacks: string[]}).fallbacks = ["openai/other"];
    }),
    mutate((config) => { finance(config).runtime = { route: "agent" }; }),
    mutate((config) => { finance(config).heartbeat = { every: "30m" }; }),
    mutate((config) => {
      (config.agents as Record<string, unknown>).defaults = { heartbeat: { every: "30m" } };
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents[0]!.subagents = { allowAgents: ["finance"] };
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents[0]!.subagents = { allowAgents: ["Finance!!!"] };
    }),
    mutate((config) => {
      const agents = config.agents as Record<string, unknown>;
      agents.defaults = { subagents: { allowAgents: ["finance"] } };
    }),
    mutate((config) => {
      const agents = config.agents as Record<string, unknown>;
      agents.defaults = { subagents: { allowAgents: ["*"] } };
    }),
    mutate((config) => {
      const agents = config.agents as Record<string, unknown>;
      agents.defaults = { subagents: { allowAgents: [" * "] } };
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents[0]!.subagents = { allowAgents: [" * "] };
    }),
    mutate((config) => { finance(config).contextInjection = "always"; }),
    mutate((config) => {
      (finance(config).memorySearch as {enabled: boolean}).enabled = true;
    }),
    mutate((config) => { finance(config).tools = { deny: [] }; }),
    mutate((config) => { finance(config).tools = { deny: [...TOOL_NAMES] }; }),
    mutate((config) => {
      (config.tools as {deny: string[]}).deny = [...TOOL_NAMES];
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents[0]!.tools = { deny: [...TOOL_NAMES] };
    }),
    mutate((config) => {
      finance(config).models = { "openai/gpt-5.6-luna": { alias: " bad " } };
    }),
    mutate((config) => {
      ((config.bindings as Array<Record<string, unknown>>)[0]!).agentId = "finance";
    }),
    mutate((config) => {
      ((config.bindings as Array<Record<string, unknown>>)[0]!).agentId = "Finance!!!";
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents.splice(0, 1);
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      delete agents[0]!.default;
    }),
    mutate((config) => {
      const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
      agents[0]!.default = false;
      agents[1]!.default = true;
    }),
    mutate((config) => { finance(config).id = "Finance"; }),
    mutate((config) => { config.hooks = { enabled: true }; }),
    mutate((config) => {
      config.hooks = {
        enabled: false,
        mappings: [{ action: "agent", agentId: "Finance!!!", model: "other/model" }],
      };
    }),
    mutate((config) => {
      config.hooks = {
        enabled: false,
        mappings: [{ action: "agent", sessionKey: "Agent:Finance:hook" }],
      };
    }),
    mutate((config) => {
      config.hooks = { enabled: false, allowedAgentIds: ["*"] };
    }),
    mutate((config) => {
      config.channels = {
        telegram: {
          groups: { "-1001": { topics: { "42": { agentId: "finance" } } } },
        },
      };
    }),
    mutate((config) => {
      config.channels = {
        telegram: {
          direct: {
            "111": {
              topics: {
                "42": { agentId: "Finance!!!", systemPrompt: "ordinary injected prompt" },
              },
            },
          },
        },
      };
    }),
    mutate((config) => {
      const llm = (((config.plugins as {entries: Record<string, unknown>}).entries[
        "finance-bridge"
      ]) as {llm: Record<string, unknown>}).llm;
      llm.allowedModels = ["openai/gpt-5.6-luna"];
    }),
  ];
  for (const [index, candidate] of cases.entries()) {
    assert.throws(
      () => validateFinanceHostPolicyV2(candidate),
      `unsafe host policy case ${index} must fail closed`,
    );
  }
});

test("reviewed plugin sources bind the loaded roots to the hashed OpenClaw closure", () => {
  const config = hostConfig();
  assert.throws(
    () => validateLoadedFinancePluginRootV2(config, "/other/finance-bridge"),
    /loaded Finance plugin root/u,
  );
  assert.throws(
    () => validateReviewedPluginSourcesV2(
      config,
      FINANCE_PLUGIN_ROOT,
      "/other-openclaw",
      CODEX_SOURCE,
      OPENAI_SOURCE,
    ),
    /reviewed Codex plugin root/u,
  );
  const reordered = structuredClone(config);
  (reordered.plugins as {load: {paths: string[]}}).load.paths = [
    CODEX_PLUGIN_ROOT,
    FINANCE_PLUGIN_ROOT,
  ];
  assert.throws(
    () => validateReviewedPluginSourcesV2(
      reordered,
      FINANCE_PLUGIN_ROOT,
      OPENCLAW_ARTIFACT_ROOT,
      CODEX_SOURCE,
      OPENAI_SOURCE,
    ),
    /loaded Finance plugin root/u,
  );
});

test("reviewed Codex source evidence refuses fallback, drift, and absent selection", () => {
  for (const candidate of [
    undefined,
    { ...CODEX_SOURCE, rootDir: "/global/node_modules/@openclaw/codex" },
    { ...CODEX_SOURCE, source: "/global/node_modules/@openclaw/codex/dist/index.js" },
    { ...CODEX_SOURCE, origin: "global" },
    { ...CODEX_SOURCE, status: "disabled" },
    { ...CODEX_SOURCE, packageName: "@other/codex" },
    { ...CODEX_SOURCE, providerIds: ["codex", "other"] },
  ]) {
    assert.throws(
      () => validateReviewedPluginSourcesV2(
        hostConfig(), FINANCE_PLUGIN_ROOT, OPENCLAW_ARTIFACT_ROOT, candidate,
        OPENAI_SOURCE,
      ),
      /loaded Codex|config-selected source/u,
    );
  }
});

test("reviewed OpenAI source evidence refuses disabled, relocated, or widened providers", () => {
  for (const candidate of [
    undefined,
    { ...OPENAI_SOURCE, rootDir: "/global/openai" },
    { ...OPENAI_SOURCE, source: "/global/openai/index.js" },
    { ...OPENAI_SOURCE, origin: "config" },
    { ...OPENAI_SOURCE, status: "disabled" },
    Object.fromEntries(Object.entries(OPENAI_SOURCE).filter(([key]) => key !== "packageName")),
    { ...OPENAI_SOURCE, packageName: "@other/openai-provider" },
    { ...OPENAI_SOURCE, extra: true },
    { ...OPENAI_SOURCE, providerIds: ["openai", "other"] },
    { ...OPENAI_SOURCE, sourceConfigEntrySha256: "a".repeat(64) },
  ]) {
    assert.throws(
      () => validateReviewedPluginSourcesV2(
        hostConfig(), FINANCE_PLUGIN_ROOT, OPENCLAW_ARTIFACT_ROOT, CODEX_SOURCE,
        candidate,
      ),
      /loaded OpenAI|reviewed bundled source/u,
    );
  }
});

test("invalid current config becomes a bounded refusal without model or raw config", () => {
  const invalid = hostConfig();
  invalid.channels = {
    telegram: {
      groups: {
        "-1001": {
          topics: {
            "42": {
              agentId: "Finance!!!",
              systemPrompt: "do-not-retain",
            },
          },
        },
      },
    },
  };
  const refusal = financeProjectionOrRefusalV2(
    invalid, evidence, "2026.7.1", CODEX_SOURCE, OPENAI_SOURCE,
  );
  assert.deepEqual(Object.keys(refusal).sort(), [
    "evidence_sha256", "refusal_code", "schema_version",
  ]);
  assert.equal(refusal.schema_version, "finance-openclaw-agent-config-refusal-v2");
  assert.equal(refusal.refusal_code, "projection_invalid");
  assert.match(refusal.evidence_sha256, /^[0-9a-f]{64}$/u);
  const encoded = JSON.stringify(refusal);
  assert.equal(encoded.includes("do-not-retain"), false);
  assert.equal(encoded.includes("gpt-5.6-luna"), false);
});
