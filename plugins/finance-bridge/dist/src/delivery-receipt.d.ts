import type { OpenClawPluginApi } from "openclaw-sdk/plugin-sdk/plugin-entry";
export declare const FINANCE_DELIVERY_CAPABILITY: "telegram.finance-delivery-material-v1";
export interface FinanceDeliveryMaterialV1 {
    readonly capability: typeof FINANCE_DELIVERY_CAPABILITY;
    readonly deliveryMaterialVersion: "finance_d2_delivery_material_v1";
    readonly attemptNonce: string;
    readonly deliveryMaterialSha256: string;
    readonly providerMessageId: string;
    readonly receiptTokenSha256: string;
    readonly channel: "telegram";
    readonly accountId: string;
    readonly conversationId: string;
    readonly sessionKey: string;
    readonly sourceIdentitySha256: string;
}
export interface FinanceDeliveryReceiptV1 {
    readonly version: "finance_delivery_receipt_v1";
    consume(consumer: (material: FinanceDeliveryMaterialV1) => void | Promise<void>): Promise<void>;
}
export type FinanceDeliveryReceiptConsumerV1 = (receipt: FinanceDeliveryReceiptV1) => void | Promise<void>;
export interface FinanceDeliveryReceiptRecorder {
    recordFinanceDeliveryReceipt(material: FinanceDeliveryMaterialV1, deadlineMs: number): Promise<void>;
}
export type FinanceDeliveryPluginApi = OpenClawPluginApi & {
    readonly financeDeliveryCapabilities?: readonly [typeof FINANCE_DELIVERY_CAPABILITY];
    registerFinanceDeliveryReceiptConsumerV1?: (consumer: FinanceDeliveryReceiptConsumerV1) => void;
};
export declare function requireFinanceDeliveryHost(api: OpenClawPluginApi): asserts api is FinanceDeliveryPluginApi & {
    readonly financeDeliveryCapabilities: readonly [typeof FINANCE_DELIVERY_CAPABILITY];
    registerFinanceDeliveryReceiptConsumerV1: (consumer: FinanceDeliveryReceiptConsumerV1) => void;
};
