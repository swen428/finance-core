import { createHash } from "node:crypto";

import {
  assertBridgeCut,
  withExclusiveBridgeCut,
  type BridgeCutOptions,
  type BridgeProfileLocator,
  type BridgeStageWrite,
} from "./bridge-export-boundary.js";
import { HandoffPublisher, type FrozenBridgeHandoffV1 } from "./handoff.js";

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
export async function exportBridgeOwnerState(
  locator: BridgeProfileLocator,
  options: BridgeCutOptions = {},
): Promise<BridgeOwnerExportReceiptV1> {
  return await withExclusiveBridgeCut(locator, async (cut, sink) => {
    const handoff = await new HandoffPublisher(cut.workspaceRoot).exportFrozen(cut, sink);
    assertBridgeCut(cut, sink);
    const manifestBytes = Buffer.from(`${JSON.stringify(handoff)}\n`, "utf8");
    const manifest = await sink.writeValidated("bridge-owner-export-v1.json", manifestBytes);
    assertBridgeCut(cut, sink);
    return Object.freeze({
      contractVersion: "finance-bridge-owner-export-receipt-v1",
      profileId: cut.profileId,
      cutId: cut.cutId,
      stagePath: cut.stagePath,
      handoff,
      manifest,
      manifestSha256: createHash("sha256").update(manifestBytes).digest("hex"),
    });
  }, options);
}
