import assert from "node:assert/strict";
import test from "node:test";

import {
  parseProcessingStatusV2,
  renderProcessingFooterV2,
  renderProcessingStatusDetailV2,
} from "../src/processing-status-v2.js";
import type { JsonObject } from "../src/protocol.js";

function status(overrides: Partial<JsonObject> = {}): JsonObject {
  return {
    intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
    attempt_public_id: null,
    receipt_public_id: null,
    admission_decision_public_id: null,
    processing_path: "no_model",
    safe_reason_code: null,
    canonical_attribution: null,
    display_alias: null,
    attribution_match: null,
    ...overrides,
  };
}

test("renderer covers deterministic, local, cloud, denial, failure, and unknown paths", () => {
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status())), "🧮 未使用模型");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "local_model", display_alias: "Qwen 3.8",
  }))), "💻 Qwen 3.8");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "cloud_projection", display_alias: "GPT Luna",
  }))), "☁️ GPT Luna");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "model_denied", safe_reason_code: "configuration_not_accepted",
  }))), "🛑 模型配置未获接纳");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "model_failed", safe_reason_code: "provider_unavailable",
  }))), "🛑 模型服务不可用");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "outcome_unknown", safe_reason_code: "outcome_unknown",
  }))), "🛑 处理结果未知");
  assert.equal(renderProcessingFooterV2(parseProcessingStatusV2(status({
    processing_path: "future_path", safe_reason_code: "provider_secret_text",
  }))), "🛑 状态暂不可用");
});

test("detail preserves canonical receipt attribution and immutable identifiers", () => {
  const parsed = parseProcessingStatusV2(status({
    attempt_public_id: `aifa_${"a".repeat(64)}`,
    receipt_public_id: `aimr_${"b".repeat(64)}`,
    processing_path: "cloud_projection",
    display_alias: "GPT Luna",
    canonical_attribution: {
      provider: "openai", model: "gpt-5.6-luna", agent_id: "finance",
    },
    attribution_match: true,
  }));
  assert.equal(renderProcessingStatusDetailV2(parsed), [
    "Finance intake: raw_intake_12345678-1234-1234-1234-123456789abc",
    "Processing path: cloud_projection",
    "Display: ☁️ GPT Luna",
    "Canonical attribution: openai/gpt-5.6-luna",
    "Agent: finance",
    `Compatibility receipt: aimr_${"b".repeat(64)}`,
    `Attempt: aifa_${"a".repeat(64)}`,
  ].join("\n"));
});

test("parser rejects extra fields, malformed identities, unsafe aliases, and spoofed Agent attribution", () => {
  const cases = [
    status({ extra: "field" }),
    status({ intake_public_id: "raw_intake_------------------------------------" }),
    status({ display_alias: "bad\nspoof" }),
    status({
      canonical_attribution: { provider: "openai", model: "gpt", agent_id: "main" },
    }),
  ];
  for (const candidate of cases) assert.throws(() => parseProcessingStatusV2(candidate));
});
