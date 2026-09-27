import { type BridgeCutOptions, type BridgeProfileLocator, type BridgeStageWrite } from "./bridge-export-boundary.js";
import { type FrozenBridgeHandoffV1 } from "./handoff.js";
export interface BridgeOwnerExportReceiptV1 {
    readonly contractVersion: "finance-bridge-owner-export-receipt-v1";
    readonly profileId: string;
    readonly cutId: string;
    readonly stagePath: string;
    readonly handoff: FrozenBridgeHandoffV1;
    readonly manifest: BridgeStageWrite;
    readonly manifestSha256: string;
}
/**
 * This freezes Bridge handoff state only. A complete D4 cut must keep this
 * same exclusive session alive while every other owner exports its state.
 */
export declare function exportBridgeOwnerState(locator: BridgeProfileLocator, options?: BridgeCutOptions): Promise<BridgeOwnerExportReceiptV1>;
