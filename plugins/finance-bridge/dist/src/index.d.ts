import type { OpenClawPluginApi } from "openclaw-sdk/plugin-sdk/plugin-entry";
import { type FinanceBridgeConfig } from "./config.js";
import { type BridgeRunner } from "./controller.js";
import { HandoffPublisher } from "./handoff.js";
import { ReceiptMediaAdapter } from "./media.js";
export declare function validateHostPolicy(value: unknown): void;
export declare function validateRetryCapability(value: unknown): void;
export interface RegistrationDependencies {
    validateConfig(value: unknown): Promise<FinanceBridgeConfig>;
    createRunner(config: FinanceBridgeConfig, markUnhealthy: () => void): BridgeRunner;
    createMediaAdapter(): ReceiptMediaAdapter | Promise<ReceiptMediaAdapter>;
    createHandoffPublisher(config: FinanceBridgeConfig, markUnhealthy: () => void): HandoffPublisher;
}
export declare function registerFinanceBridge(api: OpenClawPluginApi, dependencies?: RegistrationDependencies): void;
declare const _default: {
    id: string;
    name: string;
    description: string;
    register: typeof registerFinanceBridge;
};
export default _default;
