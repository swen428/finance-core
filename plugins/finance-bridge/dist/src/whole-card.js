const MAX_CARD_BYTES = 16_384;
const CARD_REFERENCE = /^d1card_[0-9a-f]{32}$/u;
const FIELD_ORDER = [
    "amount",
    "currency",
    "transaction_date",
    "merchant",
    "description",
    "category",
];
const LABELS = new Map([
    ["amount", "amount"],
    ["金额", "amount"],
    ["currency", "currency"],
    ["币种", "currency"],
    ["date", "transaction_date"],
    ["日期", "transaction_date"],
    ["merchant", "merchant"],
    ["商户", "merchant"],
    ["description", "description"],
    ["描述", "description"],
    ["category", "category"],
    ["分类", "category"],
]);
const REFERENCE_LABELS = new Set(["card ref", "资料卡编号"]);
function hasUnpairedSurrogate(value) {
    for (let index = 0; index < value.length; index += 1) {
        const codeUnit = value.charCodeAt(index);
        if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
            const next = value.charCodeAt(index + 1);
            if (next < 0xdc00 || next > 0xdfff)
                return true;
            index += 1;
        }
        else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
            return true;
        }
    }
    return false;
}
function containsUnsafeScalar(value, allowLineBreaks) {
    if (hasUnpairedSurrogate(value) || /[\p{Cf}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u.test(value)) {
        return true;
    }
    for (const character of value) {
        const codePoint = character.codePointAt(0);
        if (codePoint < 32 && !(character === "\t" ||
            (allowLineBreaks && (character === "\r" || character === "\n")))) {
            return true;
        }
        if (codePoint === 127)
            return true;
    }
    return false;
}
function splitLabel(line) {
    const ascii = line.indexOf(":");
    const fullWidth = line.indexOf("：");
    const positions = [ascii, fullWidth].filter((position) => position >= 0);
    if (positions.length === 0)
        throw new Error("Whole card line has no label separator.");
    const position = Math.min(...positions);
    return [
        line.slice(0, position).replace(/^[ \t]+|[ \t]+$/gu, ""),
        line.slice(position + 1).replace(/^[ \t]+|[ \t]+$/gu, ""),
    ];
}
export function parseWholeCard(text) {
    if (typeof text !== "string" || text.length === 0 || hasUnpairedSurrogate(text) ||
        Buffer.byteLength(text, "utf8") > MAX_CARD_BYTES ||
        containsUnsafeScalar(text, true)) {
        throw new Error("Whole card text is not safe bounded UTF-8 material.");
    }
    let cardReference;
    const parsed = new Map();
    for (const rawLine of text.split(/\r\n|[\n\r]/u)) {
        const line = rawLine.replace(/^[ \t]+|[ \t]+$/gu, "");
        if (line.length === 0)
            continue;
        const [label, value] = splitLabel(line);
        const canonicalLabel = label.toLocaleLowerCase("en-US");
        if (REFERENCE_LABELS.has(canonicalLabel)) {
            if (cardReference !== undefined)
                throw new Error("Whole card reference is duplicated.");
            cardReference = value;
            continue;
        }
        const field = LABELS.get(canonicalLabel);
        if (field === undefined)
            throw new Error("Whole card contains an unknown label.");
        if (parsed.has(field))
            throw new Error("Whole card field is duplicated.");
        parsed.set(field, value);
    }
    if (cardReference === undefined || !CARD_REFERENCE.test(cardReference)) {
        throw new Error("Whole card reference is missing or invalid.");
    }
    if (FIELD_ORDER.some((field) => !parsed.has(field))) {
        throw new Error("Whole card must contain every supported field exactly once.");
    }
    return {
        cardReference,
        fields: Object.fromEntries(FIELD_ORDER.map((field) => [field, parsed.get(field)])),
    };
}
function requireSafeLine(value, field) {
    if (typeof value !== "string" || containsUnsafeScalar(value, false) ||
        Buffer.byteLength(value, "utf8") > MAX_CARD_BYTES) {
        throw new Error(`Whole card ${field} cannot be rendered safely.`);
    }
    return value;
}
export function renderWholeCard(input) {
    if (!CARD_REFERENCE.test(input.cardReference)) {
        throw new Error("Whole card reference is invalid.");
    }
    if (!Array.isArray(input.unresolvedReasons) || input.unresolvedReasons.length > 32) {
        throw new Error("Whole card unresolved reasons are invalid.");
    }
    const fields = Object.fromEntries(FIELD_ORDER.map((field) => [
        field,
        requireSafeLine(input.fields[field], field),
    ]));
    const reasons = input.unresolvedReasons.map((reason) => requireSafeLine(reason, "unresolved reason"));
    const labels = input.language === "zh"
        ? {
            reference: "资料卡编号", amount: "金额", currency: "币种", date: "日期",
            merchant: "商户", description: "描述", category: "分类",
        }
        : {
            reference: "Card Ref", amount: "Amount", currency: "Currency", date: "Date",
            merchant: "Merchant", description: "Description", category: "Category",
        };
    const copyable = [
        `${labels.reference}: ${input.cardReference}`,
        `${labels.amount}: ${fields.amount}`,
        `${labels.currency}: ${fields.currency}`,
        `${labels.date}: ${fields.transaction_date}`,
        `${labels.merchant}: ${fields.merchant}`,
        `${labels.description}: ${fields.description}`,
        `${labels.category}: ${fields.category}`,
    ].join("\n");
    const status = input.language === "zh"
        ? `状态：${input.status === "publishable" ? "可确认" : "待补充"}`
        : `Status: ${input.status === "publishable" ? "ready for confirmation" : "incomplete"}`;
    const unresolved = reasons.length === 0
        ? ""
        : input.language === "zh"
            ? `\n待处理：${reasons.join(", ")}`
            : `\nUnresolved: ${reasons.join(", ")}`;
    const instruction = input.language === "zh"
        ? "复制并修改下面资料卡，再回复完整代码块；资料卡编号不要修改。"
        : "Copy and edit the card below, then reply with the complete code block. Do not change Card Ref.";
    return `${status}${unresolved}\n${instruction}\n\n\u0060\u0060\u0060text\n${copyable}\n\u0060\u0060\u0060`;
}
export function extractWholeCardReference(text) {
    if (typeof text !== "string" || text.length === 0 ||
        Buffer.byteLength(text, "utf8") > MAX_CARD_BYTES) {
        return undefined;
    }
    let found;
    for (const rawLine of text.split(/\r\n|[\n\r]/u)) {
        const line = rawLine.replace(/^[ \t]+|[ \t]+$/gu, "");
        if (line.length === 0)
            continue;
        let pair;
        try {
            pair = splitLabel(line);
        }
        catch {
            continue;
        }
        const [label, value] = pair;
        if (!REFERENCE_LABELS.has(label.toLocaleLowerCase("en-US")) || !CARD_REFERENCE.test(value)) {
            continue;
        }
        if (found !== undefined)
            return undefined;
        found = value;
    }
    return found;
}
export const EMPTY_WHOLE_CARD_FIELDS = {
    amount: "",
    currency: "",
    transaction_date: "",
    merchant: "",
    description: "",
    category: "",
};
