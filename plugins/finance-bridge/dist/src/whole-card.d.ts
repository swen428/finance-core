export type WholeCardFields = {
    amount: string;
    currency: string;
    transaction_date: string;
    merchant: string;
    description: string;
    category: string;
};
export type ParsedWholeCard = {
    cardReference: string;
    fields: WholeCardFields;
};
export declare function parseWholeCard(text: string): ParsedWholeCard;
export declare function renderWholeCard(input: {
    cardReference: string;
    fields: WholeCardFields;
    language: "zh" | "en";
    status: "incomplete" | "publishable";
    unresolvedReasons: readonly string[];
}): string;
export declare function extractWholeCardReference(text: string): string | undefined;
export declare const EMPTY_WHOLE_CARD_FIELDS: WholeCardFields;
