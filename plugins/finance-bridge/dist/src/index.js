import { createFinanceCommand } from "./command.js";
import { validatePluginConfig } from "./config.js";
import { FINANCE_FAILURE_REPLY, FinanceInboundController, } from "./controller.js";
import { HandoffPublisher } from "./handoff.js";
import { createHumanActionInteractiveHandler } from "./interactive.js";
import { ReceiptMediaAdapter } from "./media.js";
import { registerOperatorCliV1 } from "./operator-cli-v1.js";
import { financeProjectionOrRefusalV2, getLoadedCodexPluginSourceV2, getLoadedOpenAIPluginSourceV2, resolveReviewedFinanceHostRuntimeV2, validateFinanceHostPolicyV2, validateLoadedFinancePluginRootV2, } from "./finance-agent-runtime-v2.js";
import { createBridgeRequest } from "./protocol.js";
import { BridgeCliRunner } from "./subprocess.js";
import { createDisabledTools } from "./tools.js";
export function validateHostPolicy(value) {
    validateFinanceHostPolicyV2(value);
}
export function validateRetryCapability(value) {
    if (typeof value !== "object" || value === null ||
        value.capabilities?.maxRetries !== true) {
        throw new Error("Pinned OpenClaw host does not prove effective maxRetries control.");
    }
}
function hostLlmRuntime(api, config) {
    return {
        currentProjection: () => {
            const reviewed = resolveReviewedFinanceHostRuntimeV2(api.runtime);
            return financeProjectionOrRefusalV2(reviewed.hostConfig, config.agentProfileV2, api.runtime.version, reviewed.codexPluginSource, reviewed.openaiPluginSource);
        },
        complete: async (params) => {
            const completion = await api.runtime.llm.complete(params);
            return {
                text: completion.text,
                provider: completion.provider,
                model: completion.model,
                agentId: completion.agentId,
                usage: {
                    inputTokens: completion.usage.inputTokens,
                    outputTokens: completion.usage.outputTokens,
                },
                audit: {
                    caller: {
                        kind: completion.audit.caller.kind,
                        id: completion.audit.caller.id,
                        name: completion.audit.caller.name,
                    },
                    purpose: completion.audit.purpose,
                    sessionKey: completion.audit.sessionKey,
                },
            };
        },
        recordStageTiming: (stage, elapsedMs) => {
            api.logger.info(`finance_ai_stage_v2 stage=${stage} elapsed_ms=${elapsedMs}`);
        },
    };
}
const defaultDependencies = {
    validateConfig: validatePluginConfig,
    createRunner: (config, markUnhealthy) => new BridgeCliRunner(config, undefined, undefined, markUnhealthy),
    createMediaAdapter: async () => {
        const { getMediaDir } = await import("openclaw/plugin-sdk/media-runtime");
        return new ReceiptMediaAdapter(getMediaDir);
    },
    createHandoffPublisher: (config, markUnhealthy) => new HandoffPublisher(config.workspaceRoot, { markUnhealthy }),
};
export function registerFinanceBridge(api, dependencies = defaultDependencies) {
    if (api.registrationMode === "cli-metadata" ||
        api.registrationMode === "discovery" || api.registrationMode === "full") {
        registerOperatorCliV1(api);
    }
    if (api.registrationMode !== "full")
        return;
    validateRetryCapability(api.runtime.llm);
    getLoadedCodexPluginSourceV2(api.runtime);
    getLoadedOpenAIPluginSourceV2(api.runtime);
    const { hostConfig } = resolveReviewedFinanceHostRuntimeV2(api.runtime);
    validateHostPolicy(hostConfig);
    if (api.rootDir === undefined)
        throw new Error("Loaded plugin root is unavailable.");
    validateLoadedFinancePluginRootV2(hostConfig, api.rootDir);
    let healthy = false;
    let controller;
    let humanActionRuntime;
    const ready = dependencies.validateConfig(api.pluginConfig).then(async (config) => {
        const runner = dependencies.createRunner(config, () => { healthy = false; });
        const health = await runner.run(createBridgeRequest("health", { workspace_path: config.workspaceRoot }), 30_000);
        if (health.status !== "ok" || health.result.workspace_verified !== true ||
            health.result.database_verified !== true) {
            throw new Error("Finance bridge private health check failed.");
        }
        controller = new FinanceInboundController(config.workspaceRoot, runner, {
            media: await dependencies.createMediaAdapter(),
            handoff: dependencies.createHandoffPublisher(config, () => { healthy = false; }),
        }, undefined, hostLlmRuntime(api, config));
        humanActionRuntime = { workspaceRoot: config.workspaceRoot, runner };
        healthy = true;
    }).catch(() => {
        healthy = false;
        humanActionRuntime = undefined;
    });
    for (const tool of createDisabledTools(() => undefined)) {
        api.registerTool(tool, { optional: true });
    }
    api.registerCommand(createFinanceCommand(() => healthy ? humanActionRuntime : undefined));
    api.registerInteractiveHandler({
        channel: "telegram",
        namespace: "finance-bridge",
        handler: createHumanActionInteractiveHandler(() => healthy ? humanActionRuntime : undefined),
    });
    api.on("inbound_claim", async (event, context) => {
        await ready;
        if (!healthy || controller === undefined) {
            return { handled: true, reply: { text: FINANCE_FAILURE_REPLY } };
        }
        return await controller.handle(event, context);
    }, { timeoutMs: 120_000 });
}
export default {
    id: "finance-bridge",
    name: "Finance Staging Bridge",
    description: "Deterministic private Telegram-to-Finance staging orchestration.",
    register: registerFinanceBridge,
};
