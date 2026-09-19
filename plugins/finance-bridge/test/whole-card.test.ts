import assert from "node:assert/strict";
import test from "node:test";

import {
  parseWholeCard,
  renderWholeCard,
  type WholeCardFields,
} from "../src/whole-card.js";

const CARD_REFERENCE = `d1card_${"a".repeat(32)}`;
const FIELDS: WholeCardFields = {
  amount: "0012.500",
  currency: "SGD",
  transaction_date: "2026-09-19",
  merchant: "Example: Cafe",
  description: "Lunch",
  category: "Food",
};

function englishCard(overrides: Partial<WholeCardFields> = {}): string {
  const fields = { ...FIELDS, ...overrides };
  return [
    `Card Ref: ${CARD_REFERENCE}`,
    `Amount: ${fields.amount}`,
    `Currency: ${fields.currency}`,
    `Date: ${fields.transaction_date}`,
    `Merchant: ${fields.merchant}`,
    `Description: ${fields.description}`,
    `Category: ${fields.category}`,
  ].join("\n");
}

test("parses canonical Chinese, English, and mixed-label whole cards", () => {
  const chinese = [
    `资料卡编号：${CARD_REFERENCE}`,
    "金额：0012.500",
    "币种：SGD",
    "日期：2026-09-19",
    "商户：Example: Cafe",
    "描述：Lunch",
    "分类：Food",
  ].join("\n");
  assert.deepEqual(parseWholeCard(chinese), {
    cardReference: CARD_REFERENCE,
    fields: FIELDS,
  });

  const mixed = [
    `  cArD rEf ： ${CARD_REFERENCE}\t`,
    " 金额 : 0012.500 ",
    "CURRENCY：SGD",
    "日期: 2026-09-19",
    "merchant: Example: Cafe",
    "描述：Lunch",
    "CATEGORY: Food",
  ].join("\r\n");
  assert.deepEqual(parseWholeCard(mixed), {
    cardReference: CARD_REFERENCE,
    fields: FIELDS,
  });
});

test("preserves exact field strings and supports explicit empty clearing", () => {
  const parsed = parseWholeCard(englishCard({
    amount: "0012.500",
    description: "",
    category: "",
  }));
  assert.equal(parsed.fields.amount, "0012.500");
  assert.equal(parsed.fields.description, "");
  assert.equal(parsed.fields.category, "");
  assert.equal(parsed.fields.merchant, "Example: Cafe");
});

test("rejects duplicate aliases, unknown labels, and incomplete card shape", () => {
  assert.throws(() => parseWholeCard(`${englishCard()}\n金额: 12.50`));
  assert.throws(() => parseWholeCard(`${englishCard()}\nAccount: Cash`));
  assert.throws(() => parseWholeCard([
    `Card Ref: ${CARD_REFERENCE}`,
    "Amount: 12.50",
  ].join("\n")));
  assert.throws(() => parseWholeCard(`Card Ref: ${CARD_REFERENCE}`));
});

test("rejects missing, duplicate, or invalid card references", () => {
  assert.throws(() => parseWholeCard(englishCard().replace(/^Card Ref:.*\n/u, "")));
  assert.throws(() => parseWholeCard(`${englishCard()}\n资料卡编号: ${CARD_REFERENCE}`));
  assert.throws(() => parseWholeCard(englishCard().replace(CARD_REFERENCE, `d1card_${"A".repeat(32)}`)));
  assert.throws(() => parseWholeCard(englishCard().replace(CARD_REFERENCE, `d1card_${"a".repeat(31)}b:`)));
});

test("rejects unsafe Unicode, controls, and oversized input", () => {
  assert.throws(() => parseWholeCard(englishCard({ merchant: "bad\u0000value" })));
  assert.throws(() => parseWholeCard(englishCard({ merchant: "bad\u2028value" })));
  assert.throws(() => parseWholeCard(englishCard({ merchant: "bad\ud800value" })));
  assert.throws(() => parseWholeCard(englishCard({ description: "x".repeat(16_384) })));
});

test("renders all fields, a copyable card, and status without evidence material", () => {
  const zh = renderWholeCard({
    cardReference: CARD_REFERENCE,
    fields: { ...FIELDS, description: "", category: "" },
    language: "zh",
    status: "incomplete",
    unresolvedReasons: ["missing_description", "missing_category"],
  });
  assert.match(zh, /状态：待补充/u);
  assert.match(zh, /待处理：missing_description, missing_category/u);
  assert.match(zh, new RegExp(`资料卡编号: ${CARD_REFERENCE}`, "u"));
  assert.match(zh, /描述: \n分类: /u);
  assert.doesNotMatch(zh, /evidence|sha256|hash/iu);

  const en = renderWholeCard({
    cardReference: CARD_REFERENCE,
    fields: FIELDS,
    language: "en",
    status: "publishable",
    unresolvedReasons: [],
  });
  assert.match(en, /Status: ready for confirmation/u);
  assert.match(en, /Card Ref:/u);
  assert.match(en, /Amount: 0012\.500/u);
  assert.match(en, /Merchant: Example: Cafe/u);
  assert.doesNotMatch(en, /Unresolved:/u);
});

test("renderer refuses invalid references or unsafe display material", () => {
  assert.throws(() => renderWholeCard({
    cardReference: "d1card_invalid",
    fields: FIELDS,
    language: "en",
    status: "publishable",
    unresolvedReasons: [],
  }));
  assert.throws(() => renderWholeCard({
    cardReference: CARD_REFERENCE,
    fields: { ...FIELDS, merchant: "bad\nline" },
    language: "en",
    status: "publishable",
    unresolvedReasons: [],
  }));
});
