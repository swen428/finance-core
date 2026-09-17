import { Type } from "typebox";
export const ACTION_NOT_ENABLED = "ACTION_NOT_ENABLED";
export const TOOL_NAMES = [
    "finance_propose",
    "finance_get_review",
    "finance_confirm",
    "finance_edit",
    "finance_reject",
    "finance_finalize",
    "finance_get_status",
    "finance_health",
];
const strictEmptyObject = Type.Object({}, { additionalProperties: false });
export function createDisabledTools(_privateController) {
    return TOOL_NAMES.map((name) => ({
        name,
        label: name,
        description: "Finance action is registered but unavailable in S5b.",
        parameters: strictEmptyObject,
        async execute() {
            return {
                content: [{ type: "text", text: ACTION_NOT_ENABLED }],
                details: { code: ACTION_NOT_ENABLED },
            };
        },
    }));
}
