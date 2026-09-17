import type { AnyAgentTool } from "openclaw-sdk/plugin-sdk/plugin-entry";
export declare const ACTION_NOT_ENABLED = "ACTION_NOT_ENABLED";
export declare const TOOL_NAMES: readonly ["finance_propose", "finance_get_review", "finance_confirm", "finance_edit", "finance_reject", "finance_finalize", "finance_get_status", "finance_health"];
export declare function createDisabledTools(_privateController: () => void): AnyAgentTool[];
