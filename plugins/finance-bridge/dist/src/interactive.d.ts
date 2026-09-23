import type { BridgeRunner } from "./controller.js";
export declare const DISABLED_REPLY = "Current action is not enabled.";
export declare const DISABLED_ACTIONS: readonly ["edit-disabled"];
export declare const ACTIVE_ACTIONS: readonly ["post", "confirm", "edit", "reject"];
export declare const ACTION_FAILURE_REPLY = "Finance action could not be applied safely. Request a fresh review.";
export declare const ACTION_OUTCOME_UNKNOWN_REPLY: string;
export declare const POSTING_OUTCOME_UNKNOWN_REPLY: string;
export declare const POSTING_NEEDS_ATTENTION_REPLY: string;
export declare const POSTING_LOCAL_LOOKUP_REPLY: string;
export declare const EDIT_PRESENTATION_FAILURE_REPLY: string;
export type DisabledAction = (typeof DISABLED_ACTIONS)[number];
export type ActiveAction = (typeof ACTIVE_ACTIONS)[number];
export interface HumanActionRuntime {
    workspaceRoot: string;
    runner: BridgeRunner;
}
export declare function disabledCallbackData(action: DisabledAction): string;
export declare function humanActionCallbackData(action: ActiveAction, reference: string): string;
export declare function postingActionCallbackData(reference: string): string;
export declare function createHumanActionInteractiveHandler(runtime: () => HumanActionRuntime | undefined): (value: unknown) => Promise<{
    handled: true;
}>;
export declare function createDisabledInteractiveHandler(_privateController: () => void): (value: unknown) => Promise<{
    handled: true;
}>;
