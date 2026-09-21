import type { FinanceBridgeConfig } from "./config.js";
import type { FinanceDeliveryMaterialV1 } from "./delivery-receipt.js";
export declare const FINANCE_DELIVERY_RECEIPT_PROOF_VERSION: "finance_delivery_receipt_proof_v1";
export declare function financeDeliveryReceiptProofSha256(signingKey: Buffer, workspacePath: string, material: FinanceDeliveryMaterialV1): string;
export interface FinanceDeliveryReceiptProofProvider {
    validate(config: FinanceBridgeConfig): Promise<void>;
    authenticate(config: FinanceBridgeConfig, material: FinanceDeliveryMaterialV1): Promise<string>;
}
export declare const financeDeliveryReceiptProofProvider: FinanceDeliveryReceiptProofProvider;
