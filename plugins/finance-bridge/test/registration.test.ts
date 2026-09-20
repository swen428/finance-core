import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type {
  AnyAgentTool,
  OpenClawPluginApi,
  OpenClawPluginCommandDefinition,
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
  PluginHookInboundClaimResult,
} from "openclaw-sdk/plugin-sdk/plugin-entry";
import { Type } from "typebox";

import {
  registerFinanceBridge,
  validateHostPolicy,
  validateRetryCapability,
  type RegistrationDependencies,
} from "../src/index.js";
import type { FinanceBridgeConfig } from "../src/config.js";
import type { BridgeRunner } from "../src/controller.js";
import type {
  FinanceDeliveryMaterialV1,
  FinanceDeliveryReceiptConsumerV1,
} from "../src/delivery-receipt.js";
import { computeBuildSourceIdentityV1 } from "../src/artifact-hash-v1.js";
import { HandoffPublisher } from "../src/handoff.js";
import { ReceiptMediaAdapter } from "../src/media.js";
import {
  executeCompatibilityRegistrationV1,
  registerOperatorCliV1,
} from "../src/operator-cli-v1.js";
import type { CompatibilityArtifactEvidenceV1 } from "../src/model-compatibility-operator-v1.js";
import type { BridgeRequest, BridgeResponse, JsonObject } from "../src/protocol.js";

const FINANCE_PLUGIN_ROOT = "/repo/plugins/finance-bridge";
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

function hostConfig(): Record<string, unknown> {
  const denied = [
    "finance_propose", "finance_get_review", "finance_confirm", "finance_edit",
    "finance_reject", "finance_finalize", "finance_get_status", "finance_health",
    "codex_threads",
  ];
  const financeDenied = [...denied, "sessions_spawn"];
  return {
    plugins: {
      allow: ["telegram", "finance-bridge", "codex", "openai"],
      load: { paths: [FINANCE_PLUGIN_ROOT, CODEX_PLUGIN_ROOT] },
      entries: {
        codex: { enabled: true },
        openai: { enabled: true },
        "finance-bridge": {
          enabled: true,
          llm: {
            allowModelOverride: true,
            allowAgentIdOverride: true,
          },
        },
      },
    },
    tools: { deny: denied },
    agents: {
      list: [
        {
          id: "main",
          default: true,
          model: { primary: "openai/gpt-5.6-sol" },
          tools: { deny: denied },
        },
        {
          id: "finance",
          model: { primary: "openai/gpt-5.6-luna", fallbacks: [] },
          models: { "openai/gpt-5.6-luna": { alias: "GPT Luna" } },
          tools: { deny: financeDenied },
          memorySearch: { enabled: false, experimental: { sessionMemory: false } },
          contextInjection: "never",
        },
      ],
    },
    bindings: [],
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

function fakeApi(
  registrationMode:
    | "full" | "discovery" | "tool-discovery" | "setup-only" | "setup-runtime"
    | "cli-metadata" = "full",
  sourceEntrySha256Override?: string,
  openAiSourceOverride: unknown = OPENAI_SOURCE,
) {
  const tools: Array<{ tool: AnyAgentTool; options: unknown }> = [];
  const hooks: Array<{ name: string; handler: unknown; options: unknown }> = [];
  const interactive: unknown[] = [];
  const commands: OpenClawPluginCommandDefinition[] = [];
  const cli: unknown[] = [];
  const completions: unknown[] = [];
  const deliveryReceiptConsumers: FinanceDeliveryReceiptConsumerV1[] = [];
  const currentConfig = runtimeHostConfig();
  const api = {
    id: "finance-bridge",
    name: "Finance Staging Bridge",
    rootDir: FINANCE_PLUGIN_ROOT,
    source: `${FINANCE_PLUGIN_ROOT}/dist/src/index.js`,
    registrationMode,
    financeDeliveryCapabilities: ["telegram.finance-delivery-material-v1"],
    registerFinanceDeliveryReceiptConsumerV1(consumer: FinanceDeliveryReceiptConsumerV1) {
      deliveryReceiptConsumers.push(consumer);
    },
    config: currentConfig,
    pluginConfig: {
      repoRoot: "/repo",
      coreDistributionRoot: "/core-distribution",
      pythonExecutable: "/repo/.venv/bin/python",
      workspaceRoot: "/workspace",
      agentProfileV2: {
        openclawPackageSha256: "1".repeat(64),
        financeCommit: "2".repeat(40),
        coreVersion: "0.1.0",
        coreManifestSha256: "4".repeat(64),
        coreWheelSha256: "5".repeat(64),
        coreApiContractVersion: "finance-core-api-v1",
        coreMigrationLedgerDigest: "6".repeat(64),
        pluginBuildSha256: "3".repeat(64),
        executionClass: "cloud_projection",
      },
    },
    runtime: {
      version: "2026.7.1",
      config: {
        current: () => currentConfig,
      },
      pluginSources: {
        getLoaded(pluginId: string) {
          if (pluginId === "openai") return openAiSourceOverride;
          if (pluginId !== "codex") return undefined;
          return {
            ...CODEX_SOURCE,
            sourceConfigEntrySha256:
              sourceEntrySha256Override ?? CODEX_SOURCE.sourceConfigEntrySha256,
          };
        },
      },
      llm: {
        capabilities: { maxRetries: true },
        async complete(params: unknown) {
          completions.push(params);
          return {
            text: "{}",
            provider: "openai",
            model: "gpt-5.6-luna",
            agentId: "finance",
            usage: { inputTokens: 1, outputTokens: 1 },
            audit: {
              caller: { kind: "plugin", id: "finance-bridge" },
              purpose: "finance-bridge.ai-proposal-v2",
            },
          };
        },
      },
    },
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    registerTool(tool: AnyAgentTool, options: unknown) { tools.push({ tool, options }); },
    on(name: string, handler: unknown, options: unknown) { hooks.push({ name, handler, options }); },
    registerInteractiveHandler(registration: unknown) { interactive.push(registration); },
    registerCommand(command: OpenClawPluginCommandDefinition) { commands.push(command); },
    registerCli(registrar: unknown, options: unknown) { cli.push({ registrar, options }); },
  } as unknown as OpenClawPluginApi;
  return {
    api, tools, hooks, interactive, commands, cli, completions, deliveryReceiptConsumers,
  };
}

function compatibilityArtifact(
  kind: "openclaw_package" | "finance_plugin_build",
): CompatibilityArtifactEvidenceV1["plugin"] {
  return {
    policy_version: "finance-runtime-artifact-tree-v1",
    artifact_kind: kind,
    package_version: kind === "openclaw_package" ? "2026.7.1" : "0.1.0",
    artifact_sha256: "a".repeat(64),
    file_count: 1,
    byte_count: 1,
    source_identity_sha256: kind === "finance_plugin_build" ? "b".repeat(64) : null,
    entries: [],
  };
}

test("CLI metadata mode registers only the non-activating operator descriptor", () => {
  const fixture = fakeApi("cli-metadata");
  registerFinanceBridge(fixture.api, dependencies);
  assert.equal(fixture.cli.length, 1);
  assert.equal(fixture.tools.length, 0);
  assert.equal(fixture.hooks.length, 0);
  assert.equal(fixture.interactive.length, 0);
  assert.equal(fixture.commands.length, 0);
});

test("CLI discovery registers no ordinary Finance runtime surface", () => {
  const fixture = fakeApi("discovery");
  registerFinanceBridge(fixture.api, {
    async validateConfig() { throw new Error("discovery must not validate runtime config"); },
    createRunner() { throw new Error("discovery must not create a runner"); },
    createMediaAdapter() { throw new Error("discovery must not create media"); },
    createHandoffPublisher() { throw new Error("discovery must not create handoff"); },
  });
  assert.equal(fixture.cli.length, 1);
  assert.equal(fixture.tools.length, 0);
  assert.equal(fixture.hooks.length, 0);
  assert.equal(fixture.interactive.length, 0);
  assert.equal(fixture.commands.length, 0);
});

test("all non-full registration modes avoid the ordinary Finance runtime", () => {
  for (const mode of ["tool-discovery", "setup-only", "setup-runtime"] as const) {
    const fixture = fakeApi(mode);
    registerFinanceBridge(fixture.api, {
      async validateConfig() { throw new Error(`${mode} must not validate runtime config`); },
      createRunner() { throw new Error(`${mode} must not create a runner`); },
      createMediaAdapter() { throw new Error(`${mode} must not create media`); },
      createHandoffPublisher() { throw new Error(`${mode} must not create handoff`); },
    });
    assert.equal(fixture.cli.length, 0);
    assert.equal(fixture.tools.length, 0);
    assert.equal(fixture.hooks.length, 0);
    assert.equal(fixture.interactive.length, 0);
    assert.equal(fixture.commands.length, 0);
  }
});

const dependencies: RegistrationDependencies = {
  async validateConfig(value) {
    return value as RegistrationDependencies extends {validateConfig(value: unknown): Promise<infer T>} ? T : never;
  },
  createRunner() {
    return {
      async recordFinanceDeliveryReceipt(_material: FinanceDeliveryMaterialV1): Promise<void> {},
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        return {
          envelopeVersion: "v1",
          requestId: request.request_id,
          operationId: "op_0123456789abcdef0123456789abcdef",
          status: "ok",
          result: {
            workspace_verified: true,
            database_verified: true,
            callback_key_status: "present",
          },
          idempotentReplay: false,
        };
      },
    };
  },
  createMediaAdapter() {
    return new ReceiptMediaAdapter(() => "/unreachable");
  },
  createHandoffPublisher(config) {
    return new HandoffPublisher(config.workspaceRoot);
  },
};

function registrationOk(request: BridgeRequest, result: JsonObject): BridgeResponse {
  return {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: "op_0123456789abcdef0123456789abcdef",
    status: "ok",
    result,
    idempotentReplay: false,
  };
}

test("plugin registers the public pinned API surfaces and eight optional disabled tools", async () => {
  const fixture = fakeApi();
  registerFinanceBridge(fixture.api, dependencies);
  await Promise.resolve();

  assert.equal(fixture.hooks.length, 1);
  assert.equal(fixture.hooks[0]?.name, "inbound_claim");
  assert.deepEqual(fixture.hooks[0]?.options, { timeoutMs: 120_000 });
  assert.equal(fixture.tools.length, 8);
  assert.ok(fixture.tools.every(({ options }) => assert.deepEqual(options, { optional: true }) === undefined));
  assert.ok(fixture.tools.every(({ tool }) => !("outputSchema" in tool)));
  for (const { tool } of fixture.tools) {
    assert.deepEqual(tool.parameters, Type.Object({}, { additionalProperties: false }));
  }
  assert.equal(fixture.commands.length, 1);
  assert.equal(fixture.deliveryReceiptConsumers.length, 1);
  assert.equal(fixture.cli.length, 1);
  assert.deepEqual((fixture.cli[0] as {options: unknown}).options, {
    commands: ["finance-compatibility"],
    descriptors: [{
      name: "finance-compatibility",
      description: "Bounded Finance model-compatibility operator workflow.",
      hasSubcommands: true,
    }],
  });
  assert.equal(fixture.commands[0]?.name, "finance");
  assert.equal(fixture.interactive.length, 4);
  assert.deepEqual(
    fixture.interactive.map((entry) => (entry as {namespace: string}).namespace),
    ["finance-bridge", "post", "edit", "reject"],
  );
  assert.ok(fixture.interactive.every((entry) =>
    (entry as {channel: string}).channel === "telegram" &&
    (entry as {handler: unknown}).handler ===
      (fixture.interactive[0] as {handler: unknown}).handler));

  const claim = fixture.hooks[0]?.handler as (
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ) => Promise<PluginHookInboundClaimResult>;
  const refusal = await claim(
    { content: "x", channel: "telegram", isGroup: false },
    { channelId: "telegram" },
  );
  assert.deepEqual(refusal, { handled: true });

  const unhealthyFixture = fakeApi();
  registerFinanceBridge(unhealthyFixture.api, {
    ...dependencies,
    async validateConfig() { throw new Error("invalid config"); },
  });
  const unhealthyClaim = unhealthyFixture.hooks[0]?.handler as (
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ) => Promise<PluginHookInboundClaimResult>;
  assert.deepEqual(await unhealthyClaim(
    { content: "x", channel: "telegram", isGroup: false },
    { channelId: "telegram" },
  ), {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });

  const failedHealthFixture = fakeApi();
  registerFinanceBridge(failedHealthFixture.api, {
    ...dependencies,
    createRunner() {
      return {
        async run(request: BridgeRequest): Promise<BridgeResponse> {
          return {
            envelopeVersion: "v1",
            requestId: request.request_id,
            operationId: "op_0123456789abcdef0123456789abcdef",
            status: "ok",
            result: { workspace_verified: true, database_verified: false },
            idempotentReplay: false,
          };
        },
      };
    },
  });
  const failedHealthClaim = failedHealthFixture.hooks[0]?.handler as (
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ) => Promise<PluginHookInboundClaimResult>;
  assert.deepEqual(await failedHealthClaim(
    { content: "x", channel: "telegram", isGroup: false },
    { channelId: "telegram" },
  ), {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
});

test("host-owned Finance delivery receipt is consumed once by the closed Python recorder", async () => {
  const fixture = fakeApi();
  const recorded: FinanceDeliveryMaterialV1[] = [];
  registerFinanceBridge(fixture.api, {
    ...dependencies,
    createRunner() {
      return {
        async recordFinanceDeliveryReceipt(material: FinanceDeliveryMaterialV1) {
          recorded.push(material);
        },
        async run(request: BridgeRequest): Promise<BridgeResponse> {
          return registrationOk(request, {
            workspace_verified: true,
            database_verified: true,
            callback_key_status: "present",
          });
        },
      };
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
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
  let consumeCalls = 0;
  await fixture.deliveryReceiptConsumers[0]!({
    version: "finance_delivery_receipt_v1",
    async consume(consumer) {
      consumeCalls += 1;
      await consumer(material);
    },
  });
  assert.equal(consumeCalls, 1);
  assert.deepEqual(recorded, [material]);
});

test("full registration refuses a host without the terminal Finance delivery capability", () => {
  const fixture = fakeApi();
  delete (fixture.api as unknown as {financeDeliveryCapabilities?: unknown})
    .financeDeliveryCapabilities;
  assert.throws(
    () => registerFinanceBridge(fixture.api, dependencies),
    /terminal-delivery capability/u,
  );
});

test("operator CLI emits only the strict structured envelope for a refused register", async () => {
  const fixture = fakeApi();
  const outputs: unknown[] = [];
  registerOperatorCliV1(fixture.api, {
    async validateConfig() {
      throw new Error("credential-shaped-config-diagnostic");
    },
    createRunner() {
      throw new Error("runner must not be created");
    },
    async resolveOpenClawRoot() {
      throw new Error("root must not be resolved");
    },
    writeOutput(value) {
      outputs.push(value);
    },
  });

  const registrar = (fixture.cli.at(-1) as {
    registrar: (context: { program: unknown }) => void;
  }).registrar;
  let registerAction: (() => Promise<void>) | undefined;
  const artifactCommand = {
    description() { return this; },
    requiredOption() { return this; },
    action() { return this; },
  };
  const registerCommand = {
    description() { return this; },
    action(action: () => Promise<void>) {
      registerAction = action;
      return this;
    },
  };
  const rootCommand = {
    description() { return this; },
    command(name: string) {
      return name === "artifact-hash" ? artifactCommand : registerCommand;
    },
  };
  registrar({
    program: {
      command() { return rootCommand; },
    },
  });
  assert.ok(registerAction !== undefined);
  await assert.rejects(registerAction(), /Finance compatibility operator refused/u);
  assert.deepEqual(outputs, [{
    schema_version: "finance-compatibility-failure-envelope-v1",
    category: "CONFIG_REFUSED",
    phase: "config",
    timer_layer: "none",
    elapsed_ms: 0,
    timeout_triggered: false,
  }]);
  assert.doesNotMatch(JSON.stringify(outputs), /credential-shaped/u);
});

test("plugin refuses the published pinned host until retry capability is patched and explicit", () => {
  assert.throws(
    () => validateRetryCapability({ complete() {} }),
    /does not prove effective maxRetries control/u,
  );
  assert.throws(
    () => validateRetryCapability({ capabilities: { maxRetries: false }, complete() {} }),
    /does not prove effective maxRetries control/u,
  );
  assert.doesNotThrow(() => validateRetryCapability({
    capabilities: { maxRetries: true }, complete() {},
  }));
  const fixture = fakeApi();
  delete (fixture.api.runtime.llm as unknown as {capabilities?: unknown}).capabilities;
  assert.throws(
    () => registerFinanceBridge(fixture.api, dependencies),
    /does not prove effective maxRetries control/u,
  );
});

test("full registration refuses a Finance plugin loaded outside the reviewed path", () => {
  const fixture = fakeApi();
  (fixture.api as unknown as {rootDir: string}).rootDir = "/other/finance-bridge";
  assert.throws(
    () => registerFinanceBridge(fixture.api, dependencies),
    /loaded Finance plugin root/u,
  );
});

test("full registration refuses a host without loaded plugin source evidence", () => {
  const fixture = fakeApi();
  delete (fixture.api.runtime as unknown as {pluginSources?: unknown}).pluginSources;
  assert.throws(
    () => registerFinanceBridge(fixture.api, dependencies),
    /does not expose loaded plugin source evidence/u,
  );
});

test("operator refuses an unreviewed Codex source before any model call", async () => {
  const fixture = fakeApi("cli-metadata");
  await assert.rejects(
    executeCompatibilityRegistrationV1({
      api: fixture.api,
      dependencies: {
        async validateConfig(value) {
          return value as FinanceBridgeConfig;
        },
        createRunner() {
          throw new Error("runner must not be created");
        },
        async resolveOpenClawRoot() {
          return "/other-openclaw";
        },
        writeOutput() {
          throw new Error("output must not be written");
        },
      },
    }),
    /reviewed Codex plugin root/u,
  );
  assert.equal(fixture.completions.length, 0);
});

test("operator refuses a non-exact Codex source-entry fingerprint before any model call", async () => {
  const fixture = fakeApi(
    "cli-metadata",
    "3d1a5ce4ec3ea89fc17954d2cd4c192df5f3076f2fc19f97b7594001d17f837f",
  );
  await assert.rejects(
    executeCompatibilityRegistrationV1({
      api: fixture.api,
      dependencies: {
        async validateConfig(value) {
          return value as FinanceBridgeConfig;
        },
        createRunner() {
          throw new Error("runner must not be created");
        },
        async resolveOpenClawRoot() {
          throw new Error("artifact resolution must not start");
        },
        writeOutput() {
          throw new Error("output must not be written");
        },
      },
    }),
    /Source Codex plugin entry is not exactly enabled-only/u,
  );
  assert.equal(fixture.completions.length, 0);
});

test("operator refuses an unreviewed bundled OpenAI source before any model call", async () => {
  const fixture = fakeApi(
    "cli-metadata",
    undefined,
    { ...OPENAI_SOURCE, status: "disabled" },
  );
  await assert.rejects(
    executeCompatibilityRegistrationV1({
      api: fixture.api,
      dependencies: {
        async validateConfig(value) {
          return value as FinanceBridgeConfig;
        },
        createRunner() {
          throw new Error("runner must not be created");
        },
        async resolveOpenClawRoot() {
          return "/openclaw";
        },
        writeOutput() {
          throw new Error("output must not be written");
        },
      },
    }),
    /reviewed bundled source/u,
  );
  assert.equal(fixture.completions.length, 0);
});

test("operator admits distinct distribution and runtime versions through pre-provider checks", async () => {
  const fixture = fakeApi("cli-metadata");
  const evidence: CompatibilityArtifactEvidenceV1 = {
    openclaw: compatibilityArtifact("openclaw_package"),
    plugin: compatibilityArtifact("finance_plugin_build"),
    core: {
      schema: "finance-core-distribution-proof-v1",
      core_version: "0.1.0",
      core_commit: "c".repeat(40),
      manifest_sha256: "4".repeat(64),
      wheel_sha256: "5".repeat(64),
      api_contract_version: "finance-core-api-v1",
      migration_ledger_digest: "6".repeat(64),
    },
    finance_commit: "c".repeat(40),
    runtime_commit: "d".repeat(40),
  };
  const packageJson = JSON.parse(await readFile(
    new URL("../../package.json", import.meta.url),
    "utf8",
  )) as Record<string, any>;
  const platformReceipt = {
    npm_package_version: "2026.7.1-2",
    verified_supply_chain: {
      root_artifact: { package_version: "2026.7.1" },
    },
  } as Record<string, any>;
  assert.equal(packageJson.peerDependencies.openclaw, "2026.7.1-2");
  assert.equal(platformReceipt.npm_package_version, "2026.7.1-2");
  assert.equal(platformReceipt.verified_supply_chain.root_artifact.package_version, "2026.7.1");
  assert.equal(fixture.api.runtime.version, "2026.7.1");
  assert.notEqual(fixture.api.runtime.version, platformReceipt.npm_package_version);

  let artifactVerificationCount = 0;
  let platformVerificationCount = 0;
  let runnerCreateCount = 0;
  await assert.rejects(executeCompatibilityRegistrationV1({
    api: fixture.api,
    dependencies: {
      async validateConfig(value) {
        return value as FinanceBridgeConfig;
      },
      createRunner() {
        runnerCreateCount += 1;
        return {
          async run(request) {
            assert.equal(request.command, "health");
            return registrationOk(request, {
              workspace_verified: true,
              database_verified: true,
              callback_key_status: "present",
            });
          },
        };
      },
      async resolveOpenClawRoot() {
        return "/openclaw";
      },
      async verifyArtifactEvidence(params) {
        artifactVerificationCount += 1;
        assert.equal(params.expectedOpenclawVersion, "2026.7.1");
        assert.equal(evidence.openclaw.package_version, params.expectedOpenclawVersion);
        return evidence;
      },
      async verifyPlatformArtifact(params) {
        platformVerificationCount += 1;
        assert.equal(params.artifact, evidence.plugin);
        assert.equal(params.openclawArtifact, evidence.openclaw);
        return {
          artifact_sha256: evidence.plugin.artifact_sha256,
          file_count: evidence.plugin.file_count,
          byte_count: evidence.plugin.byte_count,
          compiled_runtime_sha256: "d".repeat(64),
          source_identity_sha256: "b".repeat(64),
        };
      },
      writeOutput() {
        throw new Error("output must not be written");
      },
    },
  }), /ENOENT/u);
  assert.equal(artifactVerificationCount, 2);
  assert.equal(platformVerificationCount, 1);
  assert.equal(fixture.completions.length, 0);
  assert.equal(runnerCreateCount, 1);
});

test("public compatibility entry refuses platform drift before provider or receipt", async () => {
  const evidence: CompatibilityArtifactEvidenceV1 = {
    openclaw: compatibilityArtifact("openclaw_package"),
    plugin: compatibilityArtifact("finance_plugin_build"),
    core: {
      schema: "finance-core-distribution-proof-v1",
      core_version: "0.1.0",
      core_commit: "c".repeat(40),
      manifest_sha256: "4".repeat(64),
      wheel_sha256: "5".repeat(64),
      api_contract_version: "finance-core-api-v1",
      migration_ledger_digest: "6".repeat(64),
    },
    finance_commit: "c".repeat(40),
    runtime_commit: "d".repeat(40),
  };
  for (const failure of [
    "missing receipt",
    "stale artifact hash",
    "file count drift",
    "byte count drift",
    "mode drift",
    "signature drift",
    "platform drift",
    "compiled runtime drift",
    "source provenance drift",
  ]) {
    const fixture = fakeApi("cli-metadata");
    let runnerCreateCount = 0;
    await assert.rejects(
      executeCompatibilityRegistrationV1({
        api: fixture.api,
        dependencies: {
          async validateConfig(value) {
            return value as FinanceBridgeConfig;
          },
          createRunner() {
            runnerCreateCount += 1;
            return {
              async run(request) {
                assert.equal(request.command, "health");
                return registrationOk(request, {
                  workspace_verified: true,
                  database_verified: true,
                  callback_key_status: "present",
                });
              },
            };
          },
          async resolveOpenClawRoot() {
            return "/openclaw";
          },
          async verifyArtifactEvidence() {
            return evidence;
          },
          async verifyPlatformArtifact() {
            throw new Error(`platform verifier: ${failure}`);
          },
          writeOutput() {
            throw new Error("output must not be written");
          },
        },
      }),
      new RegExp(failure, "u"),
    );
    assert.equal(fixture.completions.length, 0);
    assert.equal(runnerCreateCount, 1);
  }

  const fixture = fakeApi("cli-metadata");
  let artifactVerificationCount = 0;
  let platformVerificationCount = 0;
  let runnerCreateCount = 0;
  await assert.rejects(
    executeCompatibilityRegistrationV1({
      api: fixture.api,
      dependencies: {
        async validateConfig(value) {
          return value as FinanceBridgeConfig;
        },
        createRunner() {
          runnerCreateCount += 1;
          return {
            async run(request) {
              assert.equal(request.command, "health");
              return registrationOk(request, {
                workspace_verified: true,
                database_verified: true,
                callback_key_status: "present",
              });
            },
          };
        },
        async resolveOpenClawRoot() {
          return "/openclaw";
        },
        async verifyArtifactEvidence() {
          artifactVerificationCount += 1;
          if (artifactVerificationCount === 1) return evidence;
          return {
            ...evidence,
            plugin: { ...evidence.plugin, artifact_sha256: "d".repeat(64) },
          };
        },
        async verifyPlatformArtifact() {
          platformVerificationCount += 1;
          throw new Error("platform verifier must not run after evidence drift");
        },
        writeOutput() {
          throw new Error("output must not be written");
        },
      },
    }),
    /runtime evidence changed during pre-provider admission/u,
  );
  assert.equal(artifactVerificationCount, 2);
  assert.equal(platformVerificationCount, 0);
  assert.equal(fixture.completions.length, 0);
  assert.equal(runnerCreateCount, 1);
});

test("cross-path build removes its staging directory when a required build fails", async () => {
  const isolatedTmp = await realpath(await mkdtemp(join(tmpdir(), "finance-build-cleanup-test-")));
  const fixtureRoot = join(isolatedTmp, "fixture");
  await mkdir(join(fixtureRoot, "scripts"), { recursive: true });
  await copyFile(
    join(process.cwd(), "scripts/check-native-reproducibility.mjs"),
    join(fixtureRoot, "scripts/check-native-reproducibility.mjs"),
  );
  await copyFile(
    join(process.cwd(), "scripts/build-native.mjs"),
    join(fixtureRoot, "scripts/build-native.mjs"),
  );
  await writeFile(join(fixtureRoot, "package.json"), JSON.stringify({
    engines: { node: process.version.slice(1) },
  }));
  const childEnvironment: NodeJS.ProcessEnv = { ...process.env, TMPDIR: isolatedTmp };
  delete childEnvironment.npm_execpath;
  const result = spawnSync(process.execPath, [
    join(fixtureRoot, "scripts/check-native-reproducibility.mjs"),
  ], {
    cwd: fixtureRoot,
    env: childEnvironment,
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}\n${result.stderr}`, /Required build command failed/u);
  assert.deepEqual(
    (await readdir(isolatedTmp)).filter((entry) => entry.startsWith("finance-bridge-cross-path-")),
    [],
  );
  await rm(isolatedTmp, { recursive: true });
});

test("registration injects the typed host runtime into one inbound fallback call", async () => {
  const fixture = fakeApi();
  const runnerRequests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      runnerRequests.push(request);
      if (request.command === "health") {
        return registrationOk(request, {
          workspace_verified: true,
          database_verified: true,
          callback_key_status: "present",
        });
      }
      if (request.command === "get_guided_edit_session") {
        return registrationOk(request, {
          active: false,
          session_status: "inactive",
          final_transaction_created: false,
        });
      }
      if (request.command === "capture") {
        return registrationOk(request, {
          intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "propose") {
        return registrationOk(request, {
          proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "prepare_ai_fallback_v2") {
        return registrationOk(request, {
          attempt_public_id: `aifa_${"3".repeat(64)}`,
          receipt_public_id: `aimr_${"4".repeat(64)}`,
          config_projection_hash: "5".repeat(64),
          claim_disposition: "claim_once",
          result_not_after_ms: Date.now() + 30_000,
          request_identity: {
            request_sha256: "6".repeat(64),
            model: "openai/gpt-5.6-luna",
            agent_id: "finance",
            purpose: "finance-bridge.ai-proposal-v2",
          },
        });
      }
      if (request.command === "claim_ai_fallback_invocation_v2") {
        return registrationOk(request, {
          invocation_disposition: "invoke_once",
          call_start_not_after_ms: Date.now() + 10_000,
          model_call: {
            messages: [{ role: "user", content: "{\"projection\":true}" }],
            model: "openai/gpt-5.6-luna",
            maxTokens: 1024,
            temperature: 0,
            systemPrompt: "finance-ai-prompt",
            purpose: "finance-bridge.ai-proposal-v2",
            agentId: "finance",
          },
        });
      }
      if (request.command === "record_ai_fallback_result_v2") {
        return registrationOk(request, {
          result_status: "response_refused",
          proposal_public_id: null,
        });
      }
      throw new Error(`unexpected command ${request.command}`);
    },
  };
  const registrationDependencies: RegistrationDependencies = {
    ...dependencies,
    createRunner() { return runner; },
  };
  registerFinanceBridge(fixture.api, registrationDependencies);
  await new Promise((resolve) => setImmediate(resolve));

  const claim = fixture.hooks[0]?.handler as (
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ) => Promise<PluginHookInboundClaimResult>;
  await claim(
    {
      content: "paid SGD 12.34 at Cafe",
      timestamp: 1_750_000_000_000,
      channel: "telegram",
      accountId: "finance-account",
      conversationId: "111",
      parentConversationId: "111",
      senderId: "111",
      messageId: "20",
      isGroup: false,
      commandAuthorized: true,
      senderIsOwner: true,
      metadata: {
        from: "telegram:111",
        to: "telegram:111",
        provider: "telegram",
        surface: "telegram",
      },
    },
    {
      channelId: "telegram",
      accountId: "finance-account",
      conversationId: "111",
      senderId: "111",
      messageId: "20",
      pluginBinding: {
        bindingId: "binding-1",
        pluginId: "finance-bridge",
        pluginRoot: "/plugin",
        channel: "telegram",
        accountId: "finance-account",
        conversationId: "111",
        parentConversationId: "111",
        boundAt: 1_750_000_000,
        data: { senderId: "111" },
      },
    },
  );

  assert.equal(fixture.completions.length, 1);
  const completion = fixture.completions[0] as {
    messages: Array<{ role: string; content: string }>;
    model: string;
    maxTokens: number;
    temperature: number;
    systemPrompt: string;
    purpose: string;
    agentId: string;
    maxRetries: number;
    signal: AbortSignal;
  };
  assert.deepEqual(
    {
      messages: completion.messages,
      model: completion.model,
      maxTokens: completion.maxTokens,
      temperature: completion.temperature,
      systemPrompt: completion.systemPrompt,
      purpose: completion.purpose,
      agentId: completion.agentId,
      maxRetries: completion.maxRetries,
    },
    {
      messages: [{ role: "user", content: "{\"projection\":true}" }],
      model: "openai/gpt-5.6-luna",
      maxTokens: 1024,
      temperature: 0,
      systemPrompt: "finance-ai-prompt",
      purpose: "finance-bridge.ai-proposal-v2",
      agentId: "finance",
      maxRetries: 0,
    },
  );
  assert.equal(completion.signal instanceof AbortSignal, true);
  const resultRequests = runnerRequests.filter(
    (request) => request.command === "record_ai_fallback_result_v2",
  );
  assert.equal(resultRequests.length, 1);
  const resultArguments = resultRequests[0]?.arguments;
  assert.equal(resultArguments?.transport_outcome, "response_received");
  assert.equal(resultArguments?.returned_provider, "openai");
  assert.equal(resultArguments?.returned_model, "gpt-5.6-luna");
  assert.equal(resultArguments?.returned_agent_id, "finance");
  assert.equal(resultArguments?.audit_caller_kind, "plugin");
  assert.equal(resultArguments?.audit_caller_id, "finance-bridge");
  assert.equal(resultArguments?.audit_purpose, "finance-bridge.ai-proposal-v2");
  assert.equal(resultArguments?.usage_input_tokens, 1);
  assert.equal(resultArguments?.usage_output_tokens, 1);
  assert.equal(resultArguments?.response_byte_count, 2);
  assert.equal(typeof resultArguments?.response_utf8_b64, "string");
  assert.equal(typeof resultArguments?.response_sha256, "string");
});

test("registered inbound claim routes one whole card without model or intake fallback", async () => {
  const fixture = fakeApi();
  const requests: BridgeRequest[] = [];
  const card0 = `d1card_${"a".repeat(32)}`;
  const card1 = `d1card_${"b".repeat(32)}`;
  const proposal = `po_d1_${"c".repeat(32)}`;
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "health") {
        return registrationOk(request, {
          workspace_verified: true,
          database_verified: true,
          callback_key_status: "present",
        });
      }
      if (request.command === "apply_human_draft_card") {
        return registrationOk(request, {
          draft_public_id: `d1draft_${"1".repeat(32)}`,
          draft_version: 1,
          draft_content_hash: "2".repeat(64),
          completeness: "complete",
          reason_contributors: [],
          unresolved_flags: [],
          human_reply_evidence_public_id: `d1evidence_${"3".repeat(32)}`,
          delivery_state: "not_attempted",
          delivery_state_hash: "4".repeat(64),
          delivery_attempts: [],
          delivery_outcomes: [],
          action_issue_batch_id: "5".repeat(64),
          operation_outcome: "accepted",
          refusal_code: null,
          idempotent_replay: false,
          action_issuance_state: "not_issued",
          proposal_public_id: proposal,
          proposal_version: 0,
          proposal_content_hash: "6".repeat(64),
          card_generation_public_id: card1,
          current_card_generation_public_id: card1,
          original_operation_or_start_public_id: `d1op_${"7".repeat(32)}`,
          field_values: {
            amount: "12.50", currency: "SGD", transaction_date: "2026-09-19",
            merchant: "Cafe", description: "Lunch", category: "Food",
          },
          decision_target_proposal_public_id: proposal,
          decision_target_proposal_version: 0,
          decision_target_proposal_content_hash: "6".repeat(64),
          confirm_available: true,
          reject_available: true,
          final_transaction_created: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return registrationOk(request, {
          review_public_id: `d2rev_${"8".repeat(30)}`,
          card_generation_public_id: card1,
          proposal_public_id: proposal,
          proposal_version: 0,
          proposal_content_hash: "6".repeat(64),
          posting_path: "text",
          visible_projection: {
            amount: "12.50",
            currency: "SGD",
            transaction_date: "2026-09-19",
            merchant: "Cafe",
            account: "unspecified",
          },
          visible_projection_hash: "9".repeat(64),
          expires_at: 2_000_000_000,
          final_transaction_created: false,
        });
      }
      if (request.command === "issue_posting_review_actions") {
        const text = [
          `Card Ref: ${card1}`,
          "Amount: 12.50",
          "Currency: SGD",
          "Date: 2026-09-19",
          "Merchant: Cafe",
          "Description: Lunch",
          "Category: Food",
          "Account: Not specified",
          "No account or shared-expense details will be inferred.",
        ].join("\n");
        return registrationOk(request, {
          posting_review_public_id: `d2rev_${"8".repeat(30)}`,
          delivery_attempt_public_id: `d2send_${"1".repeat(32)}`,
          delivery_manifest_version: "finance_d2_controls_v1",
          text,
          controls: [
            { action: "confirm", label: "Confirm", row_index: 0, column_index: 0,
              callback_value: `post:fha1_${"A".repeat(24)}` },
            { action: "edit", label: "Edit", row_index: 1, column_index: 0,
              callback_value: `edit:fha1_${"C".repeat(24)}` },
            { action: "reject", label: "Reject", row_index: 1, column_index: 1,
              callback_value: `reject:fha1_${"B".repeat(24)}` },
          ],
          finance_delivery_material_sha256: "2".repeat(64),
          delivery_attempt_nonce: `d2nonce_${"3".repeat(32)}`,
          final_transaction_created: false,
        });
      }
      if (request.command === "issue_human_actions") {
        return registrationOk(request, {
          proposal_public_id: proposal,
          proposal_version: 0,
          content_hash: "6".repeat(64),
          card_generation_public_id: card1,
          actions: {
            edit: { reference: `fha1_${"C".repeat(24)}`, expiry: 2_000_000_000 },
            reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
          },
          final_transaction_created: false,
        });
      }
      if (request.command === "begin_human_draft_card_delivery") {
        return registrationOk(request, { attempt_public_id: request.arguments.attempt_public_id! });
      }
      if (request.command === "record_human_draft_card_delivery_outcome") {
        return registrationOk(request, {
          observation_public_id: request.arguments.observation_public_id!,
        });
      }
      throw new Error(`unexpected command ${request.command}`);
    },
  };
  registerFinanceBridge(fixture.api, { ...dependencies, createRunner() { return runner; } });
  await new Promise((resolve) => setImmediate(resolve));
  const claim = fixture.hooks[0]?.handler as (
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ) => Promise<PluginHookInboundClaimResult>;
  const content = [
    `Card Ref: ${card0}`, "Amount: 12.50", "Currency: SGD", "Date: 2026-09-19",
    "Merchant: Cafe", "Description: Lunch", "Category: Food",
  ].join("\n");
  const result = await claim({
    content, timestamp: 1_750_000_000_000, channel: "telegram",
    accountId: "finance-account", conversationId: "111", parentConversationId: "111",
    senderId: "111", messageId: "30", replyToId: "20", sessionKey: "binding-1", isGroup: false,
    commandAuthorized: true, senderIsOwner: true,
    metadata: { from: "telegram:111", to: "telegram:111", provider: "telegram", surface: "telegram" },
  }, {
    channelId: "telegram", accountId: "finance-account", conversationId: "111",
    senderId: "111", messageId: "30", replyToId: "20", sessionKey: "binding-1",
    pluginBinding: {
      bindingId: "binding-1", pluginId: "finance-bridge", pluginRoot: "/plugin",
      channel: "telegram", accountId: "finance-account", conversationId: "111",
      parentConversationId: "111", boundAt: 1_750_000_000, data: { senderId: "111" },
    },
  });
  assert.deepEqual(requests.map((request) => request.command), [
    "health", "apply_human_draft_card", "prepare_posting_review",
    "issue_posting_review_actions",
  ]);
  assert.equal(fixture.completions.length, 0);
  assert.match(result.reply?.text ?? "", new RegExp(card1, "u"));
  assert.equal(result.reply?.presentation, undefined);
  assert.equal(
    (result.reply?.channelData?.telegram as {
      financeDeliveryMaterialV1?: {attemptNonce?: string};
    } | undefined)?.financeDeliveryMaterialV1?.attemptNonce,
    `d2nonce_${"3".repeat(32)}`,
  );
});

test("host policy requires exclusive plugin allowlist and exact root/agent denies", () => {
  assert.doesNotThrow(() => validateHostPolicy(hostConfig()));
  const config = hostConfig();
  const pluginEntry = (
    (config.plugins as {entries: Record<string, unknown>}).entries["finance-bridge"]
  ) as {enabled: boolean; llm: Record<string, unknown>};
  assert.deepEqual(pluginEntry.llm, {
    allowModelOverride: true,
    allowAgentIdOverride: true,
  });
  const agents = (config.agents as {list: Array<Record<string, unknown>>}).list;
  assert.equal(agents.filter((agent) => agent.id === "finance").length, 1);
  assert.equal(agents[0]?.id, "main");
  assert.equal(agents[0]?.default, true);
  const financeAgent = agents.find((agent) => agent.id === "finance");
  assert.deepEqual(financeAgent?.model, {
    primary: "openai/gpt-5.6-luna", fallbacks: [],
  });
  assert.deepEqual(financeAgent?.tools, {
    deny: [...(config.tools as {deny: string[]}).deny, "sessions_spawn"],
  });
  const denied = (config.tools as {deny: string[]}).deny;
  const invalid = [
    {},
    { ...hostConfig(), plugins: { allow: ["finance-bridge", "codex"] } },
    { ...hostConfig(), plugins: { allow: ["telegram", "finance-bridge"] } },
    { ...hostConfig(), plugins: { allow: ["codex", "telegram", "finance-bridge"] } },
    {
      ...hostConfig(),
      plugins: { allow: ["telegram", "finance-bridge", "codex", "openai", "*"] },
    },
    {
      ...hostConfig(),
      plugins: {
        allow: ["telegram", "finance-bridge", "codex", "openai", "unreviewed"],
      },
    },
    {
      ...hostConfig(),
      plugins: {
        ...(hostConfig().plugins as Record<string, unknown>),
        load: { paths: [FINANCE_PLUGIN_ROOT] },
      },
    },
    {
      ...hostConfig(),
      plugins: {
        ...(hostConfig().plugins as Record<string, unknown>),
        entries: {
          ...(hostConfig().plugins as {entries: Record<string, unknown>}).entries,
          codex: { enabled: true, config: {} },
        },
      },
    },
    {
      ...hostConfig(),
      plugins: {
        ...(hostConfig().plugins as Record<string, unknown>),
        entries: {
          "finance-bridge": {
            ...(
              (hostConfig().plugins as {entries: Record<string, unknown>}).entries[
                "finance-bridge"
              ] as Record<string, unknown>
            ),
            llm: { provider: "openai", model: "openai/gpt-5.6-luna" },
          },
        },
      },
    },
    {
      ...hostConfig(),
      agents: {
        list: [{
          id: "finance",
          model: { primary: "openai/gpt-5.5", fallbacks: [] },
          tools: { deny: denied },
        }],
      },
    },
    { ...hostConfig(), tools: { deny: [] } },
    { ...hostConfig(), tools: { deny: denied.filter((name) => name !== "codex_threads") } },
    { ...hostConfig(), tools: { deny: ["*", ...denied] } },
    { ...hostConfig(), tools: { ...(hostConfig().tools as object), alsoAllow: ["finance_health"] } },
    { ...hostConfig(), tools: { ...(hostConfig().tools as object), byProvider: { openai: { allow: ["finance_health"] } } } },
    { ...hostConfig(), tools: { ...(hostConfig().tools as object), toolsBySender: { "id:111": { allow: ["finance_health"] } } } },
    { ...hostConfig(), tools: { ...(hostConfig().tools as object), sandbox: { tools: { allow: ["finance_health"] } } } },
    { ...hostConfig(), agents: { list: [{ id: "finance", tools: { deny: [] } }] } },
    {
      ...hostConfig(),
      agents: {
        defaults: { tools: { allow: ["finance_health"] } },
        list: (hostConfig().agents as {list: unknown[]}).list,
      },
    },
    { ...hostConfig(), channels: { telegram: { tools: { allow: ["finance_health"] } } } },
    {
      ...hostConfig(),
      channels: {
        telegram: {
          groups: { "-1001": { topics: { "42": { agentId: "Finance!!!" } } } },
        },
      },
    },
    {
      ...hostConfig(),
      channels: {
        telegram: {
          accounts: { finance: { toolsBySender: { "111": { allow: ["finance_health"] } } } },
        },
      },
    },
  ];
  for (const value of invalid) assert.throws(() => validateHostPolicy(value));
});

test("runtime source has no network, credential, Gateway, or SQL boundary", async () => {
  const sourceRoot = new URL("../../src/", import.meta.url);
  const names = (await readdir(sourceRoot)).filter((name) => name.endsWith(".ts"));
  const source = (await Promise.all(names.map(async (name) =>
    await readFile(new URL(name, sourceRoot), "utf8")))).join("\n");
  for (const forbidden of [
    /from\s+["']node:(?:net|http|https|tls|dgram|dns)["']/iu,
    /\bfetch\s*\(/iu,
    /\b(?:credential|password|secret|submitText)\b/iu,
    /openclaw\/plugin-sdk\/(?:gateway|config|secrets)/iu,
    /\b(?:SELECT|INSERT|UPDATE|DELETE)\s+(?:FROM|INTO|SET)\b/iu,
  ]) {
    assert.doesNotMatch(source, forbidden);
  }
  assert.doesNotMatch(source, /process\.env/iu);
});


test("public build provenance matches current source identity", async () => {
  const root = await realpath(new URL("../../", import.meta.url));
  const raw = await readFile(join(root, "dist/build-provenance-v1.json"));
  const built = JSON.parse(raw.toString("utf8"));
  const current = await computeBuildSourceIdentityV1(root);

  assert.equal(built.source_identity_sha256, current.source_identity_sha256);
  assert.equal(built.file_count, current.file_count);
  assert.equal(built.byte_count, current.byte_count);
});
