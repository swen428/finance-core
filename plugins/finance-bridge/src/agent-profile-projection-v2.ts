import { createHash } from "node:crypto";

const HASH = /^[0-9a-f]{64}$/u;
const VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/u;
const PROVIDER = /^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$/u;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$/u;
const ALIAS = /^[^\p{C}\p{Zl}\p{Zp}]{1,64}$/u;

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (typeof value !== "object" || value === null) return value;
  const source = value as Record<string, unknown>;
  return Object.fromEntries(
    Object.keys(source).sort().map((key) => [key, canonical(source[key])]),
  );
}

export function canonicalProjectionSha256V2(
  projection: FinanceAgentConfigProjectionV2,
): string {
  return createHash("sha256")
    .update(JSON.stringify(canonical(projection)), "utf8")
    .digest("hex");
}

export interface FinanceAgentConfigProjectionV2 {
  schema_version: "finance-openclaw-agent-config-projection-v2";
  openclaw_version: string;
  openclaw_package_sha256: string;
  finance_commit: string;
  plugin_build_sha256: string;
  agent_id: "finance";
  canonical_provider: string;
  canonical_model: string;
  display_alias: string;
  execution_class: "local_model" | "cloud_projection";
  fallbacks: [];
  effective_max_retries: 0;
  tool_policy_sha256: string;
  memory_policy_sha256: string;
  plugin_binding_policy_sha256: string;
  projection_policy_version: string;
  projection_policy_sha256: string;
  prompt_version: string;
  prompt_sha256: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, keys: string[], label: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label} fields are not exact.`);
  }
}

function string(value: unknown, pattern: RegExp, label: string): string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

/**
 * Build the one bounded, public-safe projection accepted by Finance.
 *
 * This is pure and deliberately not registered with OpenClaw in PR A. Its
 * caller must pass only the reviewed projection source; unknown, raw-content,
 * plugin-config and sensitive fields fail closed instead of being ignored.
 */
export function projectFinanceAgentConfigV2(sourceValue: unknown): FinanceAgentConfigProjectionV2 {
  const source = record(sourceValue, "projection source");
  exact(source, [
    "openclawVersion", "openclawPackageSha256", "financeCommit",
    "pluginBuildSha256", "agents", "policies",
  ], "projection source");
  const agents = record(source.agents, "agents");
  exact(agents, ["list"], "agents");
  if (!Array.isArray(agents.list)) throw new Error("agents.list must be an array.");
  if (agents.list.length !== 1) throw new Error("Exactly one finance Agent is required.");
  const agent = record(agents.list[0], "finance Agent");
  exact(agent, ["id", "displayAlias", "executionClass", "model"], "finance Agent");
  if (agent.id !== "finance") throw new Error("The only Agent must be finance.");
  const model = record(agent.model, "finance Agent model");
  exact(model, ["primary", "fallbacks"], "finance Agent model");
  if (typeof model.primary !== "string") throw new Error("finance model.primary is invalid.");
  const slash = model.primary.indexOf("/");
  if (slash <= 0 || slash === model.primary.length - 1) {
    throw new Error("finance model.primary must be provider/model.");
  }
  const provider = string(model.primary.slice(0, slash), PROVIDER, "canonical provider");
  const canonicalModel = string(model.primary.slice(slash + 1), MODEL, "canonical model");
  if (!Array.isArray(model.fallbacks) || model.fallbacks.length !== 0) {
    throw new Error("finance model.fallbacks must be empty.");
  }
  const alias = string(agent.displayAlias, ALIAS, "display alias");
  if (alias !== alias.trim() || Buffer.byteLength(alias, "utf8") > 64) {
    throw new Error("display alias is invalid.");
  }
  if (agent.executionClass !== "local_model" && agent.executionClass !== "cloud_projection") {
    throw new Error("execution class is invalid.");
  }
  const policies = record(source.policies, "policies");
  exact(policies, [
    "toolPolicySha256", "memoryPolicySha256", "pluginBindingPolicySha256",
    "projectionPolicyVersion", "projectionPolicySha256", "promptVersion",
    "promptSha256", "effectiveMaxRetries",
  ], "policies");
  if (policies.effectiveMaxRetries !== 0) {
    throw new Error("effective provider retries must be zero.");
  }
  return {
    schema_version: "finance-openclaw-agent-config-projection-v2",
    openclaw_version: string(source.openclawVersion, VERSION, "OpenClaw version"),
    openclaw_package_sha256: string(source.openclawPackageSha256, HASH, "OpenClaw package hash"),
    finance_commit: string(source.financeCommit, /^[0-9a-f]{40}$/u, "Finance commit"),
    plugin_build_sha256: string(source.pluginBuildSha256, HASH, "plugin build hash"),
    agent_id: "finance",
    canonical_provider: provider,
    canonical_model: canonicalModel,
    display_alias: alias,
    execution_class: agent.executionClass,
    fallbacks: [],
    effective_max_retries: 0,
    tool_policy_sha256: string(policies.toolPolicySha256, HASH, "tool policy hash"),
    memory_policy_sha256: string(policies.memoryPolicySha256, HASH, "memory policy hash"),
    plugin_binding_policy_sha256: string(
      policies.pluginBindingPolicySha256, HASH, "plugin binding policy hash",
    ),
    projection_policy_version: string(
      policies.projectionPolicyVersion, VERSION, "projection policy version",
    ),
    projection_policy_sha256: string(
      policies.projectionPolicySha256, HASH, "projection policy hash",
    ),
    prompt_version: string(policies.promptVersion, VERSION, "prompt version"),
    prompt_sha256: string(policies.promptSha256, HASH, "prompt hash"),
  };
}
