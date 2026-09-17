import type { OpenClawPluginCommandDefinition } from "openclaw-sdk/plugin-sdk/plugin-entry";
import type { BridgeRunner } from "./controller.js";
export interface FinanceCommandRuntime {
    workspaceRoot: string;
    runner: BridgeRunner;
}
type CommandAvailability = boolean | FinanceCommandRuntime | undefined;
type CommandAvailabilityProvider = () => CommandAvailability;
export declare function createFinanceCommand(availability: CommandAvailabilityProvider): OpenClawPluginCommandDefinition;
export {};
