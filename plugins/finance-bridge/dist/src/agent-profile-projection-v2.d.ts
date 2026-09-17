export declare function canonicalProjectionSha256V2(projection: FinanceAgentConfigProjectionV2): string;
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
/**
 * Build the one bounded, public-safe projection accepted by Finance.
 *
 * This is pure and deliberately not registered with OpenClaw in PR A. Its
 * caller must pass only the reviewed projection source; unknown, raw-content,
 * plugin-config and sensitive fields fail closed instead of being ignored.
 */
export declare function projectFinanceAgentConfigV2(sourceValue: unknown): FinanceAgentConfigProjectionV2;
