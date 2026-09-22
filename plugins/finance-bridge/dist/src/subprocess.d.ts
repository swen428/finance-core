import { type SpawnOptions } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { type FinanceBridgeConfig } from "./config.js";
import { type BridgeRequest, type BridgeResponse } from "./protocol.js";
import type { FinanceDeliveryMaterialV1, FinanceDeliveryReceiptRecorder } from "./delivery-receipt.js";
import { type FinanceDeliveryReceiptProofProvider } from "./delivery-receipt-proof.js";
export type BridgeProcessFailureCategory = "BRIDGE_PROCESS_ABORTED" | "BRIDGE_PROCESS_IO_FAILURE" | "BRIDGE_PROCESS_NONZERO_UNVERIFIED" | "BRIDGE_PROCESS_PROTOCOL_INVALID" | "BRIDGE_PROCESS_RESOURCE_LIMIT" | "BRIDGE_PROCESS_STARTUP_FAILED" | "BRIDGE_PROCESS_TIMEOUT";
export declare function bridgeProcessFailureCategory(error: unknown): BridgeProcessFailureCategory | undefined;
export interface ChildProcessLike {
    stdin: Writable;
    stdout: Readable;
    stderr: Readable;
    once(event: "close", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
    once(event: "error", listener: (error: Error) => void): this;
    kill(signal: NodeJS.Signals): boolean;
}
export type SpawnProcess = (executable: string, args: readonly string[], options: SpawnOptions) => ChildProcessLike;
export type RevalidatePythonExecutable = (config: FinanceBridgeConfig) => Promise<void>;
export declare class BridgeCliRunner implements FinanceDeliveryReceiptRecorder {
    private readonly config;
    private readonly spawn;
    private readonly reapGraceMs;
    private readonly markUnhealthy;
    private readonly revalidateExecutable;
    private readonly receiptProofProvider;
    private poisoned;
    constructor(config: FinanceBridgeConfig, spawn?: SpawnProcess, reapGraceMs?: number, markUnhealthy?: () => void, revalidateExecutable?: RevalidatePythonExecutable, receiptProofProvider?: FinanceDeliveryReceiptProofProvider);
    private boundedReceiptPreparation;
    validateFinanceDeliveryReceiptCapability(deadlineMs: number): Promise<void>;
    recordFinanceDeliveryReceipt(material: FinanceDeliveryMaterialV1, deadlineMs: number): Promise<void>;
    run(request: BridgeRequest, deadlineMs: number, inheritedFd?: number): Promise<BridgeResponse>;
}
