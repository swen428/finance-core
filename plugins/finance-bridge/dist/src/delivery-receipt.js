export const FINANCE_DELIVERY_CAPABILITY = "telegram.finance-delivery-material-v1";
export function requireFinanceDeliveryHost(api) {
    const candidate = api;
    if (candidate.financeDeliveryCapabilities?.length !== 1 ||
        candidate.financeDeliveryCapabilities[0] !== FINANCE_DELIVERY_CAPABILITY ||
        typeof candidate.registerFinanceDeliveryReceiptConsumerV1 !== "function") {
        throw new Error("Pinned OpenClaw host lacks the Finance terminal-delivery capability.");
    }
}
