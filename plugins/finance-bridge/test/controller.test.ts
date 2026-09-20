import assert from "node:assert/strict";
import { fstatSync } from "node:fs";
import { chmod, lstat, mkdir, realpath, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
  FINANCE_FAILURE_REPLY,
  FinanceInboundController,
  type BridgeRunner,
  type FinanceLlmCompletion,
  type FinanceLlmRuntime,
} from "../src/controller.js";
import { HandoffPublisher } from "../src/handoff.js";
import { ReceiptMediaAdapter } from "../src/media.js";
import type {
  PluginConversationBinding,
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
  PluginHookInboundClaimResult,
} from "openclaw-sdk/plugin-sdk/plugin-entry";
import type { BridgeRequest, BridgeResponse, JsonObject } from "../src/protocol.js";
import { temporaryDirectory } from "./support.js";

const binding: PluginConversationBinding = {
  bindingId: "binding-1",
  pluginId: "finance-bridge",
  pluginRoot: "/plugin",
  channel: "telegram",
  accountId: "finance-account",
  conversationId: "111",
  parentConversationId: "111",
  boundAt: 1_750_000_000,
  data: { senderId: "111" },
};

const event: PluginHookInboundClaimEvent = {
  content: "taxi to airport 35.50",
  timestamp: 1_750_000_000_000,
  channel: "telegram",
  accountId: "finance-account",
  conversationId: "111",
  parentConversationId: "111",
  senderId: "111",
  messageId: "20",
  sessionKey: "binding-1",
  isGroup: false,
  commandAuthorized: true,
  senderIsOwner: true,
  metadata: {
    from: "telegram:111",
    to: "telegram:111",
    provider: "telegram",
    surface: "telegram",
    mediaPath: undefined,
    mediaUrl: undefined,
    mediaType: undefined,
    mediaPaths: undefined,
    mediaUrls: undefined,
    mediaTypes: undefined,
  },
};

const context: PluginHookInboundClaimContext = {
  channelId: "telegram",
  accountId: "finance-account",
  conversationId: "111",
  senderId: "111",
  messageId: "20",
  sessionKey: "binding-1",
  pluginBinding: binding,
};

function ok(request: BridgeRequest, result: JsonObject): BridgeResponse {
  return {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: "op_0123456789abcdef0123456789abcdef",
    status: "ok",
    result,
    idempotentReplay: false,
  };
}

function inactiveGuidedSession(request: BridgeRequest): BridgeResponse {
  return ok(request, {
    active: false,
    session_status: "inactive",
    final_transaction_created: false,
  });
}

function issuedActions(request: BridgeRequest, proposalPublicId: string): BridgeResponse {
  const proposalVersion = request.arguments.expected_proposal_version;
  const contentHash = request.arguments.expected_content_hash;
  assert.equal(typeof proposalVersion, "number");
  assert.equal(typeof contentHash, "string");
  return ok(request, {
    proposal_public_id: proposalPublicId,
    proposal_version: proposalVersion,
    content_hash: contentHash,
    actions: {
      confirm: { reference: `fha1_${"A".repeat(24)}`, expiry: 2_000_000_000 },
      edit: { reference: `fha1_${"C".repeat(24)}`, expiry: 2_000_000_000 },
      reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
    },
    final_transaction_created: false,
  });
}

function issuedRejectAction(request: BridgeRequest, proposalPublicId: string): BridgeResponse {
  const proposalVersion = request.arguments.expected_proposal_version;
  const contentHash = request.arguments.expected_content_hash;
  assert.equal(typeof proposalVersion, "number");
  assert.equal(typeof contentHash, "string");
  return ok(request, {
    proposal_public_id: proposalPublicId,
    proposal_version: proposalVersion,
    content_hash: contentHash,
    actions: {
      reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
    },
    final_transaction_created: false,
  });
}

const D1_CARD_G0 = `d1card_${"a".repeat(32)}`;
const D1_CARD_G1 = `d1card_${"b".repeat(32)}`;
const D1_CARD_G2 = `d1card_${"c".repeat(32)}`;

function wholeCardText(cardReference = D1_CARD_G0): string {
  return [
    `Card Ref: ${cardReference}`,
    "Amount: 12.50",
    "Currency: SGD",
    "Date: 2026-09-19",
    "Merchant: Example Cafe",
    "Description: Lunch",
    "Category: Food",
  ].join("\n");
}

interface D2CardFields {
  amount: string;
  currency: string;
  transactionDate: string;
  merchant: string;
  description: string;
  category: string;
}

const DEFAULT_D2_CARD_FIELDS: D2CardFields = {
  amount: "12.50",
  currency: "SGD",
  transactionDate: "2026-09-19",
  merchant: "Example Cafe",
  description: "Lunch",
  category: "Food",
};

function d2DeliveryText(
  cardReference = D1_CARD_G1,
  fields: D2CardFields = DEFAULT_D2_CARD_FIELDS,
): string {
  return [
    `Card Ref: ${cardReference}`,
    `Amount: ${fields.amount}`,
    `Currency: ${fields.currency}`,
    `Date: ${fields.transactionDate}`,
    `Merchant: ${fields.merchant}`,
    `Description: ${fields.description}`,
    `Category: ${fields.category}`,
    "Account: Not specified",
    "No account or shared-expense details will be inferred.",
  ].join("\n");
}

function humanDraftCard(
  request: BridgeRequest,
  overrides: JsonObject = {},
): BridgeResponse {
  return ok(request, {
    draft_public_id: `d1draft_${"1".repeat(32)}`,
    draft_version: 1,
    draft_content_hash: "2".repeat(64),
    completeness: "complete",
    reason_contributors: [],
    unresolved_flags: [],
    human_reply_evidence_public_id: `d1evidence_${"3".repeat(32)}`,
    delivery_state: "not_attempted",
    delivery_state_hash: "4".repeat(64),
    delivery_attempts: [],
    delivery_outcomes: [],
    action_issue_batch_id: "5".repeat(64),
    operation_outcome: "accepted",
    refusal_code: null,
    idempotent_replay: false,
    action_issuance_state: "not_issued",
    proposal_public_id: `po_d1_${"6".repeat(32)}`,
    proposal_version: 0,
    proposal_content_hash: "7".repeat(64),
    card_generation_public_id: D1_CARD_G1,
    current_card_generation_public_id: D1_CARD_G1,
    original_operation_or_start_public_id: `d1op_${"8".repeat(32)}`,
    field_values: {
      amount: "12.50",
      currency: "SGD",
      transaction_date: "2026-09-19",
      merchant: "Example Cafe",
      description: "Lunch",
      category: "Food",
    },
    decision_target_proposal_public_id: `po_d1_${"6".repeat(32)}`,
    decision_target_proposal_version: 0,
    decision_target_proposal_content_hash: "7".repeat(64),
    confirm_available: true,
    reject_available: true,
    final_transaction_created: false,
    ...overrides,
  });
}

function guidedHumanDraftResult(
  request: BridgeRequest,
  proposalPublicId: string,
  proposalVersion: number,
  proposalContentHash: string,
  merchant = "taxi",
): JsonObject {
  const response = humanDraftCard(request, {
    proposal_public_id: proposalPublicId,
    proposal_version: proposalVersion,
    proposal_content_hash: proposalContentHash,
    decision_target_proposal_public_id: proposalPublicId,
    decision_target_proposal_version: proposalVersion,
    decision_target_proposal_content_hash: proposalContentHash,
    field_values: {
      amount: "35.50",
      currency: "SGD",
      transaction_date: "2026-08-13",
      merchant,
      description: "",
      category: "",
    },
  });
  if (response.status !== "ok") throw new Error("Guided D1 fixture failed.");
  return response.result;
}

function issuedD1Actions(request: BridgeRequest, confirmAvailable: boolean): BridgeResponse {
  const proposal = request.arguments.proposal_public_id;
  const version = request.arguments.expected_proposal_version;
  const contentHash = request.arguments.expected_content_hash;
  const generation = request.arguments.card_generation_public_id;
  assert.equal(typeof proposal, "string");
  assert.equal(typeof version, "number");
  assert.equal(typeof contentHash, "string");
  assert.equal(typeof generation, "string");
  return ok(request, {
    proposal_public_id: proposal,
    proposal_version: version,
    content_hash: contentHash,
    card_generation_public_id: generation,
    actions: Array.isArray(request.arguments.requested_actions)
      ? {
          edit: { reference: `fha1_${"C".repeat(24)}`, expiry: 2_000_000_000 },
          reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
        }
      : confirmAvailable
      ? {
          confirm: { reference: `fha1_${"A".repeat(24)}`, expiry: 2_000_000_000 },
          edit: { reference: `fha1_${"C".repeat(24)}`, expiry: 2_000_000_000 },
          reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
        }
      : {
          reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
        },
    final_transaction_created: false,
  });
}

const D2_REVIEW = `d2rev_${"9".repeat(30)}`;

function preparedD2Review(request: BridgeRequest, cardReference = D1_CARD_G1): BridgeResponse {
  return ok(request, {
    review_public_id: D2_REVIEW,
    card_generation_public_id: cardReference,
    proposal_public_id: `po_d1_${"6".repeat(32)}`,
    proposal_version: 0,
    proposal_content_hash: "7".repeat(64),
    posting_path: "text",
    visible_projection: {
      amount: "12.50",
      currency: "SGD",
      transaction_date: "2026-09-19",
      merchant: "Example Cafe",
      account: "unspecified",
    },
    visible_projection_hash: "8".repeat(64),
    expires_at: 2_000_000_000,
    final_transaction_created: false,
  });
}

function preparedGuidedD2Review(
  request: BridgeRequest,
  proposalPublicId: string,
  proposalVersion: number,
  proposalContentHash: string,
  merchant = "taxi",
): BridgeResponse {
  return ok(request, {
    review_public_id: D2_REVIEW,
    card_generation_public_id: D1_CARD_G1,
    proposal_public_id: proposalPublicId,
    proposal_version: proposalVersion,
    proposal_content_hash: proposalContentHash,
    posting_path: "text",
    visible_projection: {
      amount: "35.50",
      currency: "SGD",
      transaction_date: "2026-08-13",
      merchant,
      account: "unspecified",
    },
    visible_projection_hash: "8".repeat(64),
    expires_at: 2_000_000_000,
    final_transaction_created: false,
  });
}

function issuedD2Action(
  request: BridgeRequest,
  cardReference = D1_CARD_G1,
  fields: D2CardFields = DEFAULT_D2_CARD_FIELDS,
): BridgeResponse {
  const text = d2DeliveryText(cardReference, fields);
  return ok(request, {
    posting_review_public_id: D2_REVIEW,
    delivery_attempt_public_id: `d2send_${"1".repeat(32)}`,
    delivery_manifest_version: "finance_d2_controls_v1",
    text,
    controls: [
      { action: "confirm", label: "Confirm", row_index: 0, column_index: 0,
        callback_value: `post:fha1_${"A".repeat(24)}` },
      { action: "edit", label: "Edit", row_index: 1, column_index: 0,
        callback_value: `edit:fha1_${"C".repeat(24)}` },
      { action: "reject", label: "Reject", row_index: 1, column_index: 1,
        callback_value: `reject:fha1_${"B".repeat(24)}` },
    ],
    finance_delivery_material_sha256: "2".repeat(64),
    delivery_attempt_nonce: `d2nonce_${"3".repeat(32)}`,
    final_transaction_created: false,
  });
}

const REVIEW_HASH = "1".repeat(64);
const TEST_PROJECTION = {
  schema_version: "finance-openclaw-agent-config-projection-v2" as const,
  openclaw_version: "2026.7.1",
  openclaw_package_sha256: "1".repeat(64),
  finance_commit: "2".repeat(40),
  plugin_build_sha256: "3".repeat(64),
  agent_id: "finance" as const,
  canonical_provider: "openai",
  canonical_model: "gpt-5.6-luna",
  display_alias: "GPT Luna",
  execution_class: "cloud_projection" as const,
  fallbacks: [] as [],
  effective_max_retries: 0 as const,
  tool_policy_sha256: "4".repeat(64),
  memory_policy_sha256: "5".repeat(64),
  plugin_binding_policy_sha256: "6".repeat(64),
  projection_policy_version: "finance-openclaw-agent-projection-policy-v2",
  projection_policy_sha256: "7".repeat(64),
  prompt_version: "finance-ai-prompt-v1",
  prompt_sha256: "8".repeat(64),
};

function currentProjection() {
  return TEST_PROJECTION;
}

function reviewResult(
  request: BridgeRequest,
  proposalPublicId: string,
  overrides: JsonObject = {},
): BridgeResponse {
  return ok(request, {
    proposal_public_id: proposalPublicId,
    parse_status: "parsed_pending_confirmation",
    proposal_version: 0,
    effective_content_hash: REVIEW_HASH,
    amount: "35.50",
    currency: "SGD",
    transaction_date: "2026-08-13",
    merchant: "taxi",
    description: null,
    account: null,
    account_status: "absent",
    classification: "personal",
    source_type: "telegram_text",
    proposal_origin: "deterministic",
    ai_source_kind: null,
    ambiguity_indicators: [],
    confirm_available: true,
    final_transaction_created: false,
    ...overrides,
  });
}

const INITIAL_D2_CARD = `d2card_${"d".repeat(32)}`;
const INITIAL_D2_REVIEW = `d2rev_${"e".repeat(30)}`;

function initialD2Text(overrides: {
  amount?: string;
  currency?: string;
  transactionDate?: string;
  merchant?: string | null;
  description?: string | null;
  category?: string | null;
} = {}): string {
  return [
    `Card Ref: ${INITIAL_D2_CARD}`,
    `Amount: ${overrides.amount ?? "35.50"}`,
    `Currency: ${overrides.currency ?? "SGD"}`,
    `Date: ${overrides.transactionDate ?? "2026-08-13"}`,
    `Merchant: ${overrides.merchant ?? "taxi"}`,
    `Description: ${overrides.description ?? "Not specified"}`,
    `Category: ${overrides.category ?? "Not specified"}`,
    "Account: Not specified",
    "No account or shared-expense details will be inferred.",
  ].join("\n");
}

function initialD2Prepared(request: BridgeRequest): BridgeResponse {
  return ok(request, {
    review_public_id: INITIAL_D2_REVIEW,
    card_generation_public_id: null,
    initial_card_public_id: INITIAL_D2_CARD,
    proposal_public_id: request.arguments.proposal_public_id,
    proposal_version: 0,
    proposal_content_hash: REVIEW_HASH,
    posting_path: "text",
    visible_projection: {
      amount: "35.50", currency: "SGD", transaction_date: "2026-08-13",
      merchant: "taxi", account: "unspecified",
    },
    visible_projection_hash: "8".repeat(64),
    presentation_text: initialD2Text(),
    expires_at: 2_000_000_000,
    final_transaction_created: false,
  });
}

function initialD2Manifest(request: BridgeRequest): BridgeResponse {
  return ok(request, {
    posting_review_public_id: INITIAL_D2_REVIEW,
    delivery_attempt_public_id: `d2send_${"1".repeat(32)}`,
    delivery_manifest_version: "finance_d2_controls_v1",
    text: initialD2Text(),
    controls: [
      { action: "confirm", label: "Confirm", row_index: 0, column_index: 0,
        callback_value: `post:fha1_${"A".repeat(24)}` },
      { action: "edit", label: "Edit", row_index: 1, column_index: 0,
        callback_value: `edit:fha1_${"C".repeat(24)}` },
      { action: "reject", label: "Reject", row_index: 1, column_index: 1,
        callback_value: `reject:fha1_${"B".repeat(24)}` },
    ],
    finance_delivery_material_sha256: "2".repeat(64),
    delivery_attempt_nonce: `d2nonce_${"3".repeat(32)}`,
    final_transaction_created: false,
  });
}

function replyText(result: PluginHookInboundClaimResult): string {
  if (result.reply?.text !== undefined) return result.reply.text;
  const presentation = result.reply?.presentation;
  const first = presentation?.blocks[0];
  return first?.type === "text" ? first.text : "";
}

function capturedFailure(
  intakePublicId = "raw_intake_12345678-1234-1234-1234-123456789abc",
): PluginHookInboundClaimResult {
  return {
    handled: true,
    reply: {
      text: "Finance intake could not be processed safely. Please retry.\n\n" +
        `Finance intake: ${intakePublicId}\n` +
        "🛑 状态暂不可用",
    },
  };
}

function aiPrepared(request: BridgeRequest): BridgeResponse {
  if (request.command === "prepare_ai_fallback_v2") {
    return ok(request, {
      attempt_public_id: `aifa_${"1".repeat(64)}`,
      receipt_public_id: `aimr_${"9".repeat(64)}`,
      config_projection_hash: "a".repeat(64),
      claim_disposition: "claim_once",
      result_not_after_ms: Date.now() + 20_000,
      request_identity: {
        request_sha256: "b".repeat(64),
        model: "openai/gpt-5.6-luna",
        agent_id: "finance",
        purpose: "finance-bridge.ai-proposal-v2",
      },
    });
  }
  if (request.command === "claim_ai_fallback_invocation_v2") {
    return ok(request, {
      invocation_disposition: "invoke_once",
      call_start_not_after_ms: Date.now() + 10_000,
      model_call: {
        messages: [{ role: "user", content: "{\"projection\":true}" }],
        model: "openai/gpt-5.6-luna",
        maxTokens: 1024,
        temperature: 0,
        systemPrompt: "finance-ai-prompt",
        purpose: "finance-bridge.ai-proposal-v2",
        agentId: "finance",
      },
    });
  }
  return ok(request, {
    result_status: "proposal_created",
    proposal_public_id: "prop_bridge_686d5e5cd838efaa6565a084118bb81d",
  });
}

function aiReview(request: BridgeRequest): BridgeResponse {
  if (request.command === "capture") {
    return ok(request, {
      intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
    });
  }
  if (request.command === "propose") {
    return ok(request, {
      proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
    });
  }
  if (request.command === "get_review") {
    return reviewResult(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d", {
      proposal_origin: "ai_fallback",
      ai_source_kind: "telegram_raw_text",
    });
  }
  if (request.command === "prepare_posting_review") return initialD2Prepared(request);
  if (request.command === "issue_posting_review_actions") return initialD2Manifest(request);
  if (request.command === "issue_human_actions") {
    return issuedActions(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d");
  }
  if (request.command === "get_ai_processing_status_v2") {
    return ok(request, {
      intake_public_id: request.arguments.intake_public_id,
      attempt_public_id: `aifa_${"1".repeat(64)}`,
      receipt_public_id: `aimr_${"9".repeat(64)}`,
      admission_decision_public_id: null,
      processing_path: "cloud_projection",
      safe_reason_code: null,
      canonical_attribution: {
        provider: "openai", model: "gpt-5.6-luna", agent_id: "finance",
      },
      display_alias: "GPT Luna",
      attribution_match: true,
    });
  }
  return aiPrepared(request);
}

function aiReviewWithResultDeadline(
  request: BridgeRequest,
  resultNotAfterMs: number,
  recorded: BridgeRequest[],
): BridgeResponse {
  if (request.command === "prepare_ai_fallback_v2") {
    return ok(request, {
      attempt_public_id: `aifa_${"2".repeat(64)}`,
      receipt_public_id: `aimr_${"9".repeat(64)}`,
      config_projection_hash: "a".repeat(64),
      claim_disposition: "claim_once",
      result_not_after_ms: resultNotAfterMs,
      request_identity: {
        request_sha256: "b".repeat(64),
        model: "openai/gpt-5.6-luna",
        agent_id: "finance",
        purpose: "finance-bridge.ai-proposal-v2",
      },
    });
  }
  if (request.command === "record_ai_fallback_result_v2") {
    recorded.push(request);
    return ok(request, {
      result_status: "response_refused",
      proposal_public_id: null,
    });
  }
  if (request.command === "get_ai_processing_status_v2") {
    return ok(request, {
      intake_public_id: request.arguments.intake_public_id,
      attempt_public_id: `aifa_${"2".repeat(64)}`,
      receipt_public_id: `aimr_${"9".repeat(64)}`,
      admission_decision_public_id: null,
      processing_path: "model_failed",
      safe_reason_code: "deadline_exceeded",
      canonical_attribution: {
        provider: "openai", model: "gpt-5.6-luna", agent_id: "finance",
      },
      display_alias: "GPT Luna",
      attribution_match: null,
    });
  }
  return aiReview(request);
}

function aiReviewForBoundary(
  request: BridgeRequest,
  recorded: BridgeRequest[],
): BridgeResponse {
  if (request.command === "record_ai_fallback_result_v2") {
    recorded.push(request);
    return ok(request, {
      result_status: "response_refused",
      proposal_public_id: null,
    });
  }
  return aiReview(request);
}

test("injected host LLM completes one exact fallback request and records one result", async () => {
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      return aiReview(request);
    },
  };
  let completionCalls = 0;
  let projectionCalls = 0;
  const stages: string[] = [];
  let completionParams: Parameters<FinanceLlmRuntime["complete"]>[0] | undefined;
  const llmRuntime: FinanceLlmRuntime = {
    currentProjection() {
      projectionCalls += 1;
      return currentProjection();
    },
    async complete(params) {
      completionCalls += 1;
      completionParams = params;
      return {
        text: "{\"schema_version\":\"finance-ai-proposal-v1\"}",
        provider: "openai",
        model: "gpt-5.6-luna",
        agentId: "finance",
        usage: { inputTokens: 1, outputTokens: 1 },
        audit: {
          caller: { kind: "plugin", id: "finance-bridge", name: null },
          purpose: "finance-bridge.ai-proposal-v2",
          sessionKey: undefined,
        },
      };
    },
    recordStageTiming(stage, elapsedMs) {
      stages.push(stage);
      assert.equal(Number.isInteger(elapsedMs), true);
      assert.ok(elapsedMs >= 0 && elapsedMs <= 120_000);
    },
  };
  const controller = new FinanceInboundController(
    "/tmp/workspace",
    runner,
    undefined,
    105_000,
    llmRuntime,
  );

  const result = await controller.handle(event, context);

  assert.equal(result.handled, true);
  assert.equal(projectionCalls, 1);
  assert.equal(completionCalls, 1);
  assert.equal(completionParams?.model, "openai/gpt-5.6-luna");
  assert.equal(completionParams?.agentId, "finance");
  assert.equal(completionParams?.purpose, "finance-bridge.ai-proposal-v2");
  assert.equal(completionParams?.maxRetries, 0);
  assert.equal(completionParams?.temperature, 0);
  assert.equal(completionParams?.messages.length, 1);
  assert.deepEqual(stages, [
    "config_projection",
    "receipt_lookup_and_bridge_prepare",
    "host_completion",
    "result_persistence",
  ]);
  assert.match(replyText(result), new RegExp(`Card Ref: ${INITIAL_D2_CARD}`, "u"));
  assert.match(replyText(result), /No account or shared-expense details will be inferred\./u);
  assert.equal(
    requests.filter((request) => request.command === "record_ai_fallback_result_v2").length,
    1,
  );
});

test("invalid current Agent config records one bounded denial and never dispatches a model", async () => {
  const requests: BridgeRequest[] = [];
  let completions = 0;
  const refusal = {
    schema_version: "finance-openclaw-agent-config-refusal-v2" as const,
    refusal_code: "projection_invalid" as const,
    evidence_sha256: "d".repeat(64),
  };
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        return ok(request, {
          intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "propose") {
        return ok(request, {
          proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "prepare_ai_fallback_v2") {
        assert.deepEqual(request.arguments.config_projection, refusal);
        return {
          envelopeVersion: "v1",
          requestId: request.request_id,
          operationId: "op_0123456789abcdef0123456789abcdef",
          status: "error",
          error: {
            code: "AI_MODEL_CONFIG_NOT_ACCEPTED",
            message: "bounded refusal",
            retryable: false,
          },
        };
      }
      if (request.command === "get_ai_processing_status_v2") {
        return ok(request, {
          intake_public_id: request.arguments.intake_public_id,
          attempt_public_id: null,
          receipt_public_id: null,
          admission_decision_public_id: `aimd_${"e".repeat(64)}`,
          processing_path: "model_denied",
          safe_reason_code: "configuration_not_accepted",
          canonical_attribution: null,
          display_alias: null,
          attribution_match: null,
        });
      }
      throw new Error(`unexpected command ${request.command}`);
    },
  };
  const controller = new FinanceInboundController(
    "/tmp/workspace",
    runner,
    undefined,
    105_000,
    {
      currentProjection() { return refusal; },
      async complete() {
        completions += 1;
        throw new Error("model dispatch is forbidden");
      },
    },
  );

  const result = await controller.handle(event, context);
  assert.equal(completions, 0);
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "prepare_ai_fallback_v2", "get_ai_processing_status_v2",
  ]);
  assert.match(replyText(result), /Finance intake: raw_intake_12345678/u);
  assert.match(replyText(result), /🛑 模型配置未获接纳/u);
  assert.equal(JSON.stringify(requests).includes(event.content), true);
  assert.equal(JSON.stringify(requests[2]).includes(event.content), false);
});

test("a captured intake keeps its reference and safe footer when later processing fails", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        return ok(request, {
          intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "propose") throw new Error("private persistence detail");
      if (request.command === "get_ai_processing_status_v2") {
        return ok(request, {
          intake_public_id: request.arguments.intake_public_id,
          attempt_public_id: null,
          receipt_public_id: null,
          admission_decision_public_id: null,
          processing_path: "no_model",
          safe_reason_code: null,
          canonical_attribution: null,
          display_alias: null,
          attribution_match: null,
        });
      }
      throw new Error(`unexpected command ${request.command}`);
    },
  });

  const result = await controller.handle(event, context);
  assert.deepEqual(result, {
    handled: true,
    reply: {
      text: "Finance intake could not be processed safely. Please retry.\n\n" +
        "Finance intake: raw_intake_12345678-1234-1234-1234-123456789abc\n" +
        "🧮 未使用模型",
    },
  });
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "get_ai_processing_status_v2",
  ]);
  assert.equal(JSON.stringify(result).includes("private persistence detail"), false);
});

test("never-settling host LLM has one timeout result and no retry", async () => {
  const recorded: BridgeRequest[] = [];
  const startedAt = Date.now();
  let abortedAt: number | undefined;
  let abortEvents = 0;
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      return aiReviewWithResultDeadline(request, Date.now() + 5_050, recorded);
    },
  };
  let completionCalls = 0;
  const llmRuntime: FinanceLlmRuntime = {
    currentProjection,
    async complete({ signal }) {
      completionCalls += 1;
      return await new Promise<never>((_resolve, _reject) => {
        signal.addEventListener("abort", () => {
          abortEvents += 1;
          abortedAt = Date.now();
        }, { once: true });
      });
    },
  };
  const controller = new FinanceInboundController(
    "/tmp/workspace",
    runner,
    undefined,
    105_000,
    llmRuntime,
  );

  const result = await controller.handle(event, context);

  assert.deepEqual(result, {
    handled: true,
    reply: {
      text: "Finance intake could not be processed safely. Please retry.\n\nFinance intake: raw_intake_12345678-1234-1234-1234-123456789abc\n🛑 处理超时",
    },
  });
  assert.equal(completionCalls, 1);
  assert.equal(recorded.length, 1);
  assert.equal(recorded[0]?.arguments.transport_outcome, "timeout");
  assert.equal(recorded[0]?.arguments.failure_code, "deadline_exceeded");
  assert.equal(abortEvents, 1);
  assert.ok(abortedAt !== undefined);
  assert.ok(abortedAt - startedAt <= 30_000);
});

test("fake timer aborts at the earliest 30-second invocation bound once", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 1_000_000 });
  try {
    const recorded: BridgeRequest[] = [];
    let completionCalls = 0;
    const controller = new FinanceInboundController(
      "/tmp/workspace",
      {
        async run(request) {
          if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
          return aiReviewWithResultDeadline(request, 1_035_000, recorded);
        },
      },
      undefined,
      105_000,
      {
        currentProjection,
        async complete({ signal }) {
          completionCalls += 1;
          return await new Promise<never>((_resolve, _reject) => {
            signal.addEventListener("abort", () => undefined, { once: true });
          });
        },
      },
    );

    const pending = controller.handle(event, context);
    for (let index = 0; index < 12; index += 1) await Promise.resolve();
    assert.equal(completionCalls, 1);
    assert.equal(recorded.length, 0);

    t.mock.timers.tick(29_999);
    await Promise.resolve();
    assert.equal(recorded.length, 0);

    t.mock.timers.tick(1);
    const result = await pending;
    assert.deepEqual(result, {
      handled: true,
      reply: {
        text: "Finance intake could not be processed safely. Please retry.\n\nFinance intake: raw_intake_12345678-1234-1234-1234-123456789abc\n🛑 处理超时",
      },
    });
    assert.equal(recorded.length, 1);
    assert.equal(recorded[0]?.arguments.transport_outcome, "timeout");
    assert.equal(recorded[0]?.arguments.failure_code, "deadline_exceeded");
  } finally {
    t.mock.timers.reset();
  }
});

test("late host settlement cannot reopen the timeout terminal gate", async () => {
  for (const lateOutcome of ["resolve", "reject"] as const) {
    const recorded: BridgeRequest[] = [];
    let resolveCompletion: (() => void) | undefined;
    let rejectCompletion: ((error: Error) => void) | undefined;
    const completion = new Promise<FinanceLlmCompletion>((resolve, reject) => {
      resolveCompletion = () => resolve({
        text: "{\"late\":true}",
        provider: "openai",
        model: "gpt-5.6-luna",
        agentId: "finance",
        usage: { inputTokens: 1, outputTokens: 1 },
        audit: {
          caller: { kind: "plugin", id: "finance-bridge" },
          purpose: "finance-bridge.ai-proposal-v2",
        },
      });
      rejectCompletion = reject;
    });
    const controller = new FinanceInboundController(
      "/tmp/workspace",
      {
        async run(request) {
          if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
          return aiReviewWithResultDeadline(request, Date.now() + 5_050, recorded);
        },
      },
      undefined,
      105_000,
      {
        currentProjection,
        async complete() {
          return await completion;
        },
      },
    );

    await controller.handle(event, context);
    assert.equal(recorded.length, 1);
    assert.equal(recorded[0]?.arguments.transport_outcome, "timeout");
    if (lateOutcome === "resolve") {
      resolveCompletion?.();
    } else {
      rejectCompletion?.(new Error("late host failure"));
    }
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(recorded.length, 1);
    assert.equal(recorded[0]?.arguments.transport_outcome, "timeout");
  }
});

test("host response retention uses exact byte and UTF-16 boundaries", async () => {
  const cases = [
    { text: "a".repeat(65_536), outcome: "response_received", byteCount: 65_536 },
    { text: "a".repeat(65_537), outcome: "response_oversize", byteCount: 65_537 },
    { text: "a".repeat(131_072), outcome: "response_oversize", byteCount: 131_072 },
    { text: "a".repeat(131_073), outcome: "response_resource_refused", byteCount: undefined },
  ] as const;

  for (const testCase of cases) {
    const recorded: BridgeRequest[] = [];
    const controller = new FinanceInboundController(
      "/tmp/workspace",
      {
        async run(request) {
          if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
          return aiReviewForBoundary(request, recorded);
        },
      },
      undefined,
      105_000,
      {
        currentProjection,
        async complete() {
          return {
            text: testCase.text,
            provider: "openai",
            model: "gpt-5.6-luna",
            agentId: "finance",
            usage: { inputTokens: 1, outputTokens: 1 },
            audit: {
              caller: { kind: "plugin", id: "finance-bridge", name: null },
              purpose: "finance-bridge.ai-proposal-v2",
            },
          };
        },
      },
    );

    await controller.handle(event, context);

    assert.equal(recorded.length, 1);
    assert.equal(recorded[0]?.arguments.transport_outcome, testCase.outcome);
    assert.equal(recorded[0]?.arguments.response_byte_count, testCase.byteCount);
    assert.equal(
      recorded[0]?.arguments.response_code_unit_count,
      testCase.outcome === "response_received" ? undefined : testCase.text.length,
    );
    if (testCase.outcome === "response_received") {
      assert.equal(typeof recorded[0]?.arguments.response_utf8_b64, "string");
      assert.equal(typeof recorded[0]?.arguments.response_sha256, "string");
      assert.equal(recorded[0]?.arguments.response_code_unit_count, undefined);
    } else if (testCase.outcome === "response_oversize") {
      assert.equal(recorded[0]?.arguments.response_utf8_b64, undefined);
      assert.equal(typeof recorded[0]?.arguments.response_sha256, "string");
      assert.equal(typeof recorded[0]?.arguments.response_code_unit_count, "number");
    } else {
      assert.equal(recorded[0]?.arguments.response_utf8_b64, undefined);
      assert.equal(recorded[0]?.arguments.response_sha256, undefined);
      assert.equal(recorded[0]?.arguments.response_byte_count, undefined);
      assert.equal(typeof recorded[0]?.arguments.response_code_unit_count, "number");
    }
  }
});

test("classification-only fallback stops before deterministic review or human actions", async () => {
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "record_ai_fallback_result_v2") {
        return ok(request, {
          result_status: "classification_only",
          proposal_public_id: null,
        });
      }
      return aiReview(request);
    },
  };
  const llmRuntime: FinanceLlmRuntime = {
    currentProjection,
    async complete() {
      return {
        text: "{}",
        provider: "openai",
        model: "gpt-5.6-luna",
        agentId: "finance",
        usage: {},
        audit: {
          caller: { kind: "plugin", id: "finance-bridge", name: null },
          purpose: "finance-bridge.ai-proposal-v2",
        },
      };
    },
  };
  const controller = new FinanceInboundController(
    "/tmp/workspace",
    runner,
    undefined,
    105_000,
    llmRuntime,
  );

  const result = await controller.handle(event, context);

  assert.equal(result.handled, true);
  assert.match(replyText(result), /could not verify this message/u);
  assert.deepEqual(
    requests.map((request) => request.command),
    ["capture", "propose", "prepare_ai_fallback_v2", "claim_ai_fallback_invocation_v2",
      "record_ai_fallback_result_v2", "get_ai_processing_status_v2"],
  );
});

test("unresolved review honors confirm_available and issues reject only", async () => {
  const requests: BridgeRequest[] = [];
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        return ok(request, {
          intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "propose") {
        return ok(request, { proposal_public_id: proposalPublicId });
      }
      if (request.command === "get_review") {
        return reviewResult(request, proposalPublicId, {
          ambiguity_indicators: ["missing_currency"],
          confirm_available: false,
        });
      }
      if (request.command === "issue_human_actions") {
        return ok(request, {
          proposal_public_id: proposalPublicId,
          proposal_version: 0,
          content_hash: REVIEW_HASH,
          actions: {
            reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
          },
          final_transaction_created: false,
        });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);

  const result = await controller.handle(event, context);
  const buttons = result.reply?.presentation?.blocks.find((block) => block.type === "buttons");

  assert.equal(result.handled, true);
  if (buttons?.type !== "buttons") throw new Error("Buttons block is missing.");
  assert.equal(buttons.buttons.length, 1);
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "get_review", "get_ai_processing_status_v2", "issue_human_actions",
  ]);
});

test("bound private text is captured, proposed, reviewed, and claimed without model dispatch", async () => {
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        return ok(request, {
          intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "propose") {
        return ok(request, {
          proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
        });
      }
      if (request.command === "issue_human_actions") {
        return issuedRejectAction(
          request,
          "parser_output_12345678-1234-1234-1234-123456789abc",
        );
      }
      return reviewResult(request, "parser_output_12345678-1234-1234-1234-123456789abc", {
        currency: null,
        transaction_date: null,
        merchant: "taxi",
        account: "travel-wallet",
        account_status: "present",
        ambiguity_indicators: ["missing_currency", "missing_date"],
        confirm_available: false,
      });
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);

  const result = await controller.handle(event, context);

  assert.equal(result.handled, true);
  assert.match(replyText(result), /35\.50/u);
  assert.match(replyText(result), /Account: set "travel-wallet"/u);
  assert.match(replyText(result), /Classification: personal/u);
  assert.match(replyText(result), /Source: text/u);
  assert.match(replyText(result), /Ambiguity: missing_currency, missing_date/u);
  assert.match(replyText(result), /remains unresolved and cannot be confirmed/u);
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "get_review", "get_ai_processing_status_v2", "issue_human_actions",
  ]);
  assert.equal(requests[0]?.idempotency_key, "raw-intake:telegram:111:20");
  assert.equal(
    requests[1]?.idempotency_key,
    "bridge-propose:raw_intake_12345678-1234-1234-1234-123456789abc",
  );
  assert.equal(requests[2]?.idempotency_key, undefined);
  assert.equal(requests[4]?.arguments.expected_proposal_version, 0);
  assert.equal(requests[4]?.arguments.expected_content_hash, REVIEW_HASH);
  assert.deepEqual(requests[0]?.arguments.telegram_message, {
    message_id: 20,
    date: 1_750_000_000,
    chat: { id: 111, type: "private" },
    from: { id: 111 },
    text: "taxi to airport 35.50",
  });
  assert.equal(requests[0]?.arguments.authenticated_actor_id, "111");
  assert.equal(requests[0]?.arguments.telegram_account_id, "finance-account");
  assert.equal(requests[0]?.arguments.telegram_conversation_id, "111");
  assert.equal(requests[0]?.arguments.conversation_binding_id, "binding-1");
});

test("whole-card reply uses one atomic D1 command and returns generation-bound actions", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (request.command === "apply_human_draft_card") return humanDraftCard(request);
      if (request.command === "prepare_posting_review") return preparedD2Review(request);
      if (request.command === "issue_posting_review_actions") return issuedD2Action(request);
      if (request.command === "issue_human_actions") return issuedD1Actions(request, true);
      if (request.command === "begin_human_draft_card_delivery") {
        return ok(request, { attempt_public_id: request.arguments.attempt_public_id });
      }
      if (request.command === "record_human_draft_card_delivery_outcome") {
        return ok(request, { observation_public_id: request.arguments.observation_public_id });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  });
  const cardEvent = {
    ...event,
    content: wholeCardText(),
    messageId: "30",
    replyToId: "20",
    replyToIdFull: "telegram:111:20",
  };
  const cardContext = {
    ...context,
    messageId: "30",
    replyToId: "20",
    replyToIdFull: "telegram:111:20",
  };
  const result = await controller.handle(cardEvent, cardContext);
  assert.deepEqual(requests.map((request) => request.command), [
    "apply_human_draft_card",
    "prepare_posting_review",
    "issue_posting_review_actions",
  ]);
  const applied = requests[0]!;
  assert.equal(applied.arguments.card_generation_public_id, D1_CARD_G0);
  assert.equal(applied.arguments.telegram_message_id, 30);
  assert.equal(applied.arguments.raw_card_text, wholeCardText());
  assert.deepEqual(applied.arguments.field_values, {
    amount: "12.50", currency: "SGD", transaction_date: "2026-09-19",
    merchant: "Example Cafe", description: "Lunch", category: "Food",
  });
  assert.match(String(applied.arguments.operation_public_id), /^d1op_[0-9a-f]{32}$/u);
  assert.equal(
    applied.idempotency_key,
    `bridge-human-draft-apply:${String(applied.arguments.operation_public_id)}`,
  );
  assert.equal(requests[1]?.arguments.card_generation_public_id, D1_CARD_G1);
  assert.equal(requests[2]?.arguments.posting_review_public_id, D2_REVIEW);
  assert.equal(result.reply?.presentation, undefined);
  assert.match(replyText(result), /Account: Not specified/u);
  const telegram = result.reply?.channelData?.telegram as {
    buttons?: Array<Array<{text: string; callback_data: string}>>;
    financeDeliveryMaterialV1?: {attemptNonce: string};
  } | undefined;
  assert.deepEqual(telegram?.buttons?.map((row) => row.map((button) => button.text)), [
    ["Confirm"], ["Edit", "Reject"],
  ]);
  assert.equal(telegram?.financeDeliveryMaterialV1?.attemptNonce, `d2nonce_${"3".repeat(32)}`);
  assert.equal(requests.some((request) => request.command === "capture"), false);
  assert.match(replyText(result), new RegExp(`Card Ref: ${D1_CARD_G1}`, "u"));
});

test("D2 terminal card refuses a host session that differs from the private binding", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (request.command === "apply_human_draft_card") return humanDraftCard(request);
      throw new Error(`Unexpected command ${request.command}`);
    },
  });
  const mismatchedEvent = {
    ...event,
    content: wholeCardText(),
    messageId: "30",
    replyToId: "20",
    replyToIdFull: "telegram:111:20",
    sessionKey: "different-session",
  };
  const mismatchedContext = {
    ...context,
    messageId: "30",
    replyToId: "20",
    replyToIdFull: "telegram:111:20",
    sessionKey: "different-session",
  };

  assert.deepEqual(await controller.handle(mismatchedEvent, mismatchedContext), {
    handled: true,
    reply: { text: FINANCE_FAILURE_REPLY },
  });
  assert.deepEqual(requests.map((request) => request.command), ["apply_human_draft_card"]);
});

test("incomplete D1 cards expose Reject only and reply identity mismatches fail closed", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (request.command === "apply_human_draft_card") {
        return humanDraftCard(request, {
          completeness: "incomplete",
          proposal_public_id: null,
          proposal_version: null,
          proposal_content_hash: null,
          unresolved_flags: ["missing_amount"],
          confirm_available: false,
        });
      }
      if (request.command === "issue_human_actions") return issuedD1Actions(request, false);
      if (request.command === "begin_human_draft_card_delivery") {
        return ok(request, { attempt_public_id: request.arguments.attempt_public_id });
      }
      if (request.command === "record_human_draft_card_delivery_outcome") {
        return ok(request, { observation_public_id: request.arguments.observation_public_id });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  });
  const result = await controller.handle(
    { ...event, content: wholeCardText(), messageId: "31", replyToId: "20" },
    { ...context, messageId: "31", replyToId: "20" },
  );
  const buttons = result.reply?.presentation?.blocks.find((block) => block.type === "buttons");
  if (buttons?.type !== "buttons") throw new Error("D1 buttons missing");
  assert.deepEqual(buttons.buttons.map((button) => button.label), ["Reject"]);
  assert.match(replyText(result), /Status: incomplete/u);
  assert.match(replyText(result), /Unresolved: missing_amount/u);

  requests.length = 0;
  const mismatch = await controller.handle(
    { ...event, content: wholeCardText(), messageId: "32", replyToId: "20" },
    { ...context, messageId: "32", replyToId: "21" },
  );
  assert.deepEqual(mismatch, { handled: true });
  assert.equal(requests.length, 0);
});

test("malformed card with a valid D1 reference reaches Python refusal evidence path", async () => {
  const requests: BridgeRequest[] = [];
  let recordedRefusals = 0;
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (String(request.arguments.raw_card_text).includes("Account: Cash")) {
        recordedRefusals += 1;
        return humanDraftCard(request, {
          completeness: "incomplete",
          proposal_public_id: null,
          proposal_version: null,
          proposal_content_hash: null,
          unresolved_flags: ["missing_amount"],
          operation_outcome: "refused",
          refusal_code: "D1_UNKNOWN_FIELD",
          confirm_available: false,
          idempotent_replay: recordedRefusals > 1,
        });
      }
      return {
        envelopeVersion: "v1",
        requestId: request.request_id,
        operationId: "op_0123456789abcdef0123456789abcdef",
        status: "error",
        error: { code: "HUMAN_DRAFT_ARGUMENTS_REFUSED", message: "refused", retryable: false },
      };
    },
  });
  const malformedCards = [
    `${wholeCardText()}\nAccount: Cash`,
    wholeCardText().replace("Example Cafe", "bad\u0000value"),
    wholeCardText().replace("Example Cafe", "bad\ud800value"),
  ];
  for (const [index, malformed] of malformedCards.entries()) {
    const messageId = String(33 + index);
    const result = await controller.handle(
      { ...event, content: malformed, messageId },
      { ...context, messageId },
    );
    assert.equal(requests.at(-1)?.arguments.raw_card_text, malformed);
    assert.equal(requests.at(-1)?.arguments.card_generation_public_id, D1_CARD_G0);
    assert.match(replyText(result), /card edit was refused/u);
  }
  const exactReplay = await controller.handle(
    { ...event, content: malformedCards[0]!, messageId: "33" },
    { ...context, messageId: "33" },
  );
  assert.match(replyText(exactReplay), /card edit was refused/u);
  assert.deepEqual(
    requests.map((request) => request.command),
    [
      "apply_human_draft_card", "apply_human_draft_card",
      "apply_human_draft_card", "apply_human_draft_card",
    ],
  );
  assert.equal(requests.some((request) => request.command === "issue_human_actions"), false);
});

test("unknown delivery replay queries first and returns only the bounded successor generation", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (request.command === "apply_human_draft_card" ||
          request.command === "get_human_draft_card") {
        return humanDraftCard(request, {
          delivery_state: "unknown",
          idempotent_replay: true,
        });
      }
      if (request.command === "reissue_human_draft_card") {
        return humanDraftCard(request, {
          card_generation_public_id: D1_CARD_G2,
          current_card_generation_public_id: D1_CARD_G2,
          action_issue_batch_id: "9".repeat(64),
          delivery_state: "not_attempted",
          idempotent_replay: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return preparedD2Review(request, D1_CARD_G2);
      }
      if (request.command === "issue_posting_review_actions") {
        return issuedD2Action(request, D1_CARD_G2);
      }
      if (request.command === "issue_human_actions") return issuedD1Actions(request, true);
      if (request.command === "begin_human_draft_card_delivery") {
        return ok(request, { attempt_public_id: request.arguments.attempt_public_id });
      }
      if (request.command === "record_human_draft_card_delivery_outcome") {
        return ok(request, { observation_public_id: request.arguments.observation_public_id });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  });
  const result = await controller.handle(
    { ...event, content: wholeCardText(), messageId: "34" },
    { ...context, messageId: "34" },
  );
  assert.deepEqual(requests.map((request) => request.command), [
    "apply_human_draft_card",
    "get_human_draft_card",
    "reissue_human_draft_card",
    "prepare_posting_review",
    "issue_posting_review_actions",
  ]);
  assert.equal(requests[2]?.arguments.expected_current_generation_public_id, D1_CARD_G1);
  assert.equal(requests[2]?.arguments.reason, "unknown_after_query");
  assert.equal(requests[3]?.arguments.card_generation_public_id, D1_CARD_G2);
  assert.match(replyText(result), new RegExp(`Card Ref: ${D1_CARD_G2}`, "u"));
  assert.doesNotMatch(replyText(result), new RegExp(D1_CARD_G1, "u"));
});

test("replay after a completed reissue renders the durable current winner without G3", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      requests.push(request);
      if (request.command === "apply_human_draft_card") {
        return humanDraftCard(request, {
          current_card_generation_public_id: D1_CARD_G2,
          delivery_state: "unknown",
          idempotent_replay: true,
          confirm_available: false,
          reject_available: false,
        });
      }
      if (request.command === "get_human_draft_card") {
        return humanDraftCard(request, {
          card_generation_public_id: D1_CARD_G2,
          current_card_generation_public_id: D1_CARD_G2,
          action_issue_batch_id: "9".repeat(64),
          delivery_state: "not_attempted",
          idempotent_replay: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return preparedD2Review(request, D1_CARD_G2);
      }
      if (request.command === "issue_posting_review_actions") {
        return issuedD2Action(request, D1_CARD_G2);
      }
      if (request.command === "issue_human_actions") return issuedD1Actions(request, true);
      if (request.command === "begin_human_draft_card_delivery") {
        return ok(request, { attempt_public_id: request.arguments.attempt_public_id });
      }
      if (request.command === "record_human_draft_card_delivery_outcome") {
        return ok(request, { observation_public_id: request.arguments.observation_public_id });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  });
  const result = await controller.handle(
    { ...event, content: wholeCardText(), messageId: "36" },
    { ...context, messageId: "36" },
  );
  assert.deepEqual(requests.map((request) => request.command), [
    "apply_human_draft_card",
    "get_human_draft_card",
    "prepare_posting_review",
    "issue_posting_review_actions",
  ]);
  assert.equal(requests[1]?.arguments.card_generation_public_id, D1_CARD_G2);
  assert.equal(requests.some((request) => request.command === "reissue_human_draft_card"), false);
  assert.match(replyText(result), new RegExp(`Card Ref: ${D1_CARD_G2}`, "u"));
});

test("structured guided edits bypass intake and completion emits a fresh review card", async () => {
  const session = `gedit_${"2".repeat(32)}`;
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  let proposalVersion = 0;
  let proposalHash = REVIEW_HASH;
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "get_guided_edit_session") {
        return ok(request, {
          active: true,
          session_status: "active",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: proposalVersion,
          effective_content_hash: proposalHash,
          expires_at: 2_000_000_000,
          recovery_required: false,
          final_transaction_created: false,
        });
      }
      if (request.command === "apply_guided_edit_update") {
        proposalVersion = 1;
        proposalHash = "2".repeat(64);
        return ok(request, {
          edit_kind: "completion",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: proposalVersion,
          effective_content_hash: proposalHash,
          parse_status: "edited_pending_confirmation",
          final_transaction_created: false,
        });
      }
      if (request.command === "complete_guided_edit") {
        return ok(request, {
          active: false,
          session_status: "completed_replay",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: proposalVersion,
          effective_content_hash: proposalHash,
          expires_at: 2_000_000_000,
          recovery_required: false,
          review_batch_id: "7".repeat(32),
          human_draft_card: guidedHumanDraftResult(
            request, proposal, proposalVersion, proposalHash, "Example Cafe",
          ),
          final_transaction_created: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return preparedGuidedD2Review(
          request, proposal, proposalVersion, proposalHash, "Example Cafe",
        );
      }
      if (request.command === "issue_posting_review_actions") {
        return issuedD2Action(request, D1_CARD_G1, {
          ...DEFAULT_D2_CARD_FIELDS,
          amount: "35.50",
          transactionDate: "2026-08-13",
          merchant: "Example Cafe",
          description: "Not specified",
          category: "Not specified",
        });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);
  const updateEvent = { ...event, content: "商户=Example Cafe", messageId: "30" };
  const updateContext = { ...context, messageId: "30" };
  const updated = await controller.handle(updateEvent, updateContext);
  assert.match(replyText(updated), /merchant was updated/u);
  assert.deepEqual(requests.map((request) => request.command), [
    "get_guided_edit_session", "apply_guided_edit_update",
  ]);
  assert.equal(requests[1]?.idempotency_key, `bridge-guided-edit-update:${session}:30`);
  assert.equal(requests[1]?.arguments.field_name, "merchant");
  assert.equal(requests[1]?.arguments.field_value, "Example Cafe");

  requests.length = 0;
  const completeEvent = { ...event, content: "完成", messageId: "31" };
  const completeContext = { ...context, messageId: "31" };
  const completed = await controller.handle(completeEvent, completeContext);
  const telegram = completed.reply?.channelData?.telegram as {
    buttons?: Array<Array<{text: string}>>;
  } | undefined;
  assert.deepEqual(telegram?.buttons?.flat().map((button) => button.text), [
    "Confirm", "Edit", "Reject",
  ]);
  assert.deepEqual(requests.map((request) => request.command), [
    "get_guided_edit_session", "complete_guided_edit",
    "prepare_posting_review", "issue_posting_review_actions",
  ]);
  assert.equal(requests[1]?.idempotency_key, `bridge-guided-edit-complete:${session}:31`);
  assert.match(replyText(completed), /Merchant: Example Cafe/u);
});

test("restart-safe guided routing refuses natural, excluded, and media inputs before intake", async () => {
  const session = `gedit_${"3".repeat(32)}`;
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command !== "get_guided_edit_session") {
        throw new Error(`Unexpected command ${request.command}`);
      }
      return ok(request, {
        active: true,
        session_status: "active",
        session_public_id: session,
        proposal_public_id: proposal,
        proposal_version: 0,
        effective_content_hash: REVIEW_HASH,
        expires_at: 2_000_000_000,
        recovery_required: false,
        final_transaction_created: false,
      });
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);
  for (const [index, content] of [
    "金额错了改成SGD 321.89", "account=wallet", "tax=8%",
  ].entries()) {
    const messageId = String(40 + index);
    const result = await controller.handle(
      { ...event, content, messageId },
      { ...context, messageId },
    );
    assert.match(replyText(result), /Edit was not applied/u);
  }
  const mediaResult = await controller.handle({
    ...event,
    content: "",
    messageId: "43",
    metadata: { ...event.metadata, mediaUrl: "media://inbound/receipt_123" },
  }, { ...context, messageId: "43" });
  assert.match(replyText(mediaResult), /not accepted during editing/u);
  assert.deepEqual(requests.map((request) => request.command), [
    "get_guided_edit_session", "get_guided_edit_session",
    "get_guided_edit_session", "get_guided_edit_session",
  ]);
});

test("reserved guided syntax without a session never becomes Finance intake", async () => {
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);
  for (const [index, content] of ["金额=321.89", "tax=8%", "完成"].entries()) {
    const messageId = String(50 + index);
    const result = await controller.handle(
      { ...event, content, messageId },
      { ...context, messageId },
    );
    assert.match(replyText(result), /No active Finance edit session/u);
  }
  assert.deepEqual(requests.map((request) => request.command), [
    "get_guided_edit_session", "get_guided_edit_session", "get_guided_edit_session",
  ]);
});

test("completion redelivery uses the durable current review capability generation", async () => {
  const session = `gedit_${"4".repeat(32)}`;
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "get_guided_edit_session" ||
          request.command === "complete_guided_edit") {
        const reviewBatchId = request.command === "complete_guided_edit"
          ? "4".repeat(32)
          : undefined;
        return ok(request, {
          active: false,
          session_status: "completed_replay",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: 1,
          effective_content_hash: "2".repeat(64),
          expires_at: 2_000_000_000,
          recovery_required: false,
          ...(reviewBatchId === undefined ? {} : { review_batch_id: reviewBatchId }),
          ...(reviewBatchId === undefined ? {} : {
            human_draft_card: guidedHumanDraftResult(
              request, proposal, 1, "2".repeat(64),
            ),
          }),
          final_transaction_created: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return preparedGuidedD2Review(request, proposal, 1, "2".repeat(64));
      }
      if (request.command === "issue_posting_review_actions") {
        return issuedD2Action(request, D1_CARD_G1, {
          ...DEFAULT_D2_CARD_FIELDS,
          amount: "35.50",
          transactionDate: "2026-08-13",
          merchant: "taxi",
          description: "Not specified",
          category: "Not specified",
        });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const completeEvent = { ...event, content: "完成", messageId: "60" };
  const completeContext = { ...context, messageId: "60" };
  const first = await new FinanceInboundController("/tmp/workspace", runner)
    .handle(completeEvent, completeContext);
  const second = await new FinanceInboundController("/tmp/workspace", runner)
    .handle(completeEvent, completeContext);
  assert.match(replyText(first), /Amount: 35\.50/u);
  assert.match(replyText(second), /Amount: 35\.50/u);
  const issuances = requests.filter(
    (request) => request.command === "issue_posting_review_actions",
  );
  assert.equal(issuances.length, 2);
  assert.equal(issuances[0]?.arguments.posting_review_public_id, D2_REVIEW);
  assert.equal(issuances[1]?.arguments.posting_review_public_id, D2_REVIEW);
  assert.equal(issuances[0]?.idempotency_key, issuances[1]?.idempotency_key);
  assert.equal(requests.some((request) => request.command === "capture"), false);
});

test("guided completion uses D2 delivery and never renews a legacy action batch", async () => {
  const session = `gedit_${"6".repeat(32)}`;
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "get_guided_edit_session") {
        return ok(request, {
          active: false,
          session_status: "completed_replay",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: 1,
          effective_content_hash: "2".repeat(64),
          expires_at: 2_000_000_000,
          recovery_required: false,
          final_transaction_created: false,
        });
      }
      if (request.command === "complete_guided_edit") {
        return ok(request, {
          active: false,
          session_status: "completed_replay",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: 1,
          effective_content_hash: "2".repeat(64),
          expires_at: 2_000_000_000,
          recovery_required: false,
          review_batch_id: "4".repeat(32),
          human_draft_card: guidedHumanDraftResult(
            request, proposal, 1, "2".repeat(64),
          ),
          final_transaction_created: false,
        });
      }
      if (request.command === "prepare_posting_review") {
        return preparedGuidedD2Review(request, proposal, 1, "2".repeat(64));
      }
      if (request.command === "issue_posting_review_actions") {
        return issuedD2Action(request, D1_CARD_G1, {
          ...DEFAULT_D2_CARD_FIELDS,
          amount: "35.50",
          transactionDate: "2026-08-13",
          merchant: "taxi",
          description: "Not specified",
          category: "Not specified",
        });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const result = await new FinanceInboundController("/tmp/workspace", runner).handle(
    { ...event, content: "完成", messageId: "61" },
    { ...context, messageId: "61" },
  );
  assert.match(replyText(result), /Amount: 35\.50/u);
  const completionsSeen = requests.filter((request) => request.command === "complete_guided_edit");
  assert.equal(completionsSeen.length, 1);
  assert.equal(requests.some((request) => request.command === "issue_human_actions"), false);
  assert.deepEqual(requests.map((request) => request.command), [
    "get_guided_edit_session", "complete_guided_edit",
    "prepare_posting_review", "issue_posting_review_actions",
  ]);
});

test("guided mutation responses must remain bound to the exact session material", async () => {
  const session = `gedit_${"5".repeat(32)}`;
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  const requests: BridgeRequest[] = [];
  const runner: BridgeRunner = {
    async run(request) {
      requests.push(request);
      if (request.command === "get_guided_edit_session") {
        return ok(request, {
          active: true,
          session_status: "active",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: 0,
          effective_content_hash: REVIEW_HASH,
          expires_at: 2_000_000_000,
          recovery_required: false,
          final_transaction_created: false,
        });
      }
      if (request.command === "apply_guided_edit_update") {
        return ok(request, {
          edit_kind: "completion",
          session_public_id: session,
          proposal_public_id: proposal,
          proposal_version: 1,
          effective_content_hash: "not-a-hash",
          parse_status: "edited_pending_confirmation",
          final_transaction_created: false,
        });
      }
      throw new Error(`Unexpected command ${request.command}`);
    },
  };
  const result = await new FinanceInboundController("/tmp/workspace", runner).handle(
    { ...event, content: "商户=Example", messageId: "70" },
    { ...context, messageId: "70" },
  );
  assert.equal(replyText(result), FINANCE_FAILURE_REPLY);
  assert.equal(requests.some((request) => request.command === "capture"), false);
});

test("bound private receipt without caption is retained, captured, proposed, and reviewed", async () => {
  await using fixture = await temporaryDirectory();
  const workspaceRoot = join(fixture.path, "workspace");
  await mkdir(workspaceRoot, { mode: 0o700 });
  await chmod(workspaceRoot, 0o700);
  const workspace = await realpath(workspaceRoot);
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
  const mediaRoot = join(fixture.path, "media");
  await mkdir(join(mediaRoot, "inbound"), { recursive: true, mode: 0o700 });
  await writeFile(join(mediaRoot, "inbound", "receipt_123"), jpeg, { mode: 0o600 });
  const media = new ReceiptMediaAdapter(() => mediaRoot);
  const requests: BridgeRequest[] = [];
  const inheritedFds: number[] = [];
  const runner: BridgeRunner = {
    async run(request, _deadline, inheritedFd) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        assert.notEqual(inheritedFd, undefined);
        const status = fstatSync(inheritedFd!);
        assert.equal(status.isFile(), true);
        assert.equal(status.mode & 0o777, 0o600);
        inheritedFds.push(inheritedFd!);
        return ok(request, {
          intake_public_id: "raw_intake_bridge_686d5e5cd838efaa6565a084118bb81d",
        });
      }
      if (request.command === "propose") {
        return ok(request, {
          proposal_public_id: "prop_bridge_686d5e5cd838efaa6565a084118bb81d",
        });
      }
      if (request.command === "issue_human_actions") {
        return issuedRejectAction(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d");
      }
      return reviewResult(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d", {
        parse_status: "ocr_pending_confirmation",
        source_type: "telegram_image",
        ambiguity_indicators: [
          "currency_not_determined",
          "merchant_not_determined",
          "transaction_date_not_found",
        ],
        confirm_available: false,
      });
    },
  };
  const controller = new FinanceInboundController(workspace, runner, {
    media,
    handoff: new HandoffPublisher(workspace),
  });

  const result = await controller.handle({
    ...event,
    content: "",
    metadata: {
      ...event.metadata,
      mediaUrl: "media://inbound/receipt_123",
      mediaType: "image/jpeg",
      originalFilename: "receipt.jpg",
    },
  }, context);

  assert.equal(result.handled, true);
  assert.match(replyText(result), /35\.50/u);
  assert.match(
    replyText(result),
    /currency_not_determined, merchant_not_determined, transaction_date_not_found/u,
  );
  assert.equal(inheritedFds.length, 1);
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "get_review", "get_ai_processing_status_v2", "issue_human_actions",
  ]);
  assert.deepEqual(requests[0]?.arguments, {
    workspace_path: workspace,
    kind: "receipt_image",
    handoff_filename: "raw_intake_bridge_686d5e5cd838efaa6565a084118bb81d.jpg",
    handoff_descriptor_fd: 3,
    handoff_content_hash: "474ebe266cd7f9ed28807fa3fdfe0c04cdb3cef9313cdda5c08b15910fcc8184",
    telegram_message_id: 20,
    telegram_chat_id: 111,
    telegram_message_date: 1_750_000_000,
    sender_id: 111,
    authenticated_actor_id: "111",
    telegram_account_id: "finance-account",
    telegram_conversation_id: "111",
    conversation_binding_id: "binding-1",
    declared_mime_type: "image/jpeg",
    original_filename: "receipt.jpg",
  });

  await unlink(join(mediaRoot, "inbound", "receipt_123"));
  const replay = await controller.handle({
    ...event,
    content: "",
    metadata: {
      ...event.metadata,
      mediaUrl: "media://inbound/receipt_123",
      mediaType: "image/jpeg",
      originalFilename: "receipt.jpg",
    },
  }, context);
  assert.equal(replay.handled, true);
  assert.match(replyText(replay), /35\.50/u);
  assert.deepEqual(requests.slice(5).map((request) => request.command), [
    "capture", "propose", "get_review", "get_ai_processing_status_v2", "issue_human_actions",
  ]);
  assert.equal(requests[5]?.arguments.original_filename, "receipt.jpg");

  const wrongMime = await controller.handle({
    ...event,
    content: "",
    metadata: {
      ...event.metadata,
      mediaUrl: "media://inbound/receipt_123",
      mediaType: "image/png",
      originalFilename: "receipt.jpg",
    },
  }, context);
  assert.deepEqual(wrongMime, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.equal(requests.length, 10);

  const wrongExtension = await controller.handle({
    ...event,
    content: "",
    metadata: {
      ...event.metadata,
      mediaUrl: "media://inbound/receipt_123",
      mediaType: "image/jpeg",
      originalFilename: "receipt.png",
    },
  }, context);
  assert.deepEqual(wrongExtension, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.equal(requests.length, 10);
});

test("claim refuses group, identity mismatch, commands, missing binding, and malformed metadata before CLI", async () => {
  let calls = 0;
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run() {
      calls += 1;
      throw new Error("unreachable");
    },
  });
  const cases: Array<[PluginHookInboundClaimEvent, PluginHookInboundClaimContext]> = [
    [{ ...event, isGroup: true }, context],
    [{ ...event, isGroup: undefined as unknown as false }, context],
    [{ ...event, isGroup: null as unknown as false }, context],
    [{ ...event, parentConversationId: "999" }, context],
    [{ ...event, senderId: "222" }, context],
    [{ ...event, content: "/finance status" }, context],
    [event, { ...context, pluginBinding: undefined }],
    [{ ...event, timestamp: undefined }, context],
    [{ ...event, timestamp: 1_000 }, context],
    [{ ...event, senderId: "0" }, { ...context, senderId: "0" }],
    [{ ...event, conversationId: "-111", senderId: "-111" }, {
      ...context,
      conversationId: "-111",
      senderId: "-111",
      pluginBinding: { ...binding, conversationId: "-111", data: { senderId: "-111" } },
    }],
    [{ ...event, threadId: 1 }, context],
  ];
  for (const [candidateEvent, candidateContext] of cases) {
    const result = await controller.handle(candidateEvent, candidateContext);
    assert.equal(result.handled, true);
    assert.equal(result.reply, undefined);
  }
  assert.equal(calls, 0);

  const typeOnly = await controller.handle({
    ...event,
    metadata: { ...event.metadata, mediaType: "image/jpeg" },
  }, context);
  assert.deepEqual(typeOnly, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });

  const mapperEmptyArrays = await controller.handle({
    ...event,
    metadata: { ...event.metadata, mediaUrls: [], mediaPaths: [], mediaTypes: [] },
  }, context);
  assert.deepEqual(mapperEmptyArrays, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  for (const metadata of [
    { ...event.metadata, mediaUrl: 123 },
    { ...event.metadata, mediaUrls: {} },
    { ...event.metadata, mediaStagingPending: "true" },
    { ...event.metadata, originalFilename: "receipt.jpg" },
  ]) {
    const malformedEvent = { ...event, metadata } as unknown as PluginHookInboundClaimEvent;
    assert.deepEqual(await controller.handle(malformedEvent, context), {
      handled: true,
      reply: { text: "Finance intake could not be processed safely. Please retry." },
    });
  }
  assert.equal(calls, 6);
});

test("receipt date outside Python datetime range is refused before media or handoff", async () => {
  let calls = 0;
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run() {
      calls += 1;
      throw new Error("unreachable");
    },
  });
  const result = await controller.handle({
    ...event,
    timestamp: 253_402_300_800_000,
    content: "",
    metadata: {
      ...event.metadata,
      mediaUrl: "media://inbound/receipt",
      mediaType: "image/jpeg",
    },
  }, context);
  assert.equal(result.handled, true);
  assert.equal(result.reply, undefined);
  assert.equal(calls, 0);
});

test("controller serializes same-workspace turns and returns bounded handled refusal", async () => {
  let active = 0;
  let maximum = 0;
  const runner: BridgeRunner = {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, 2));
      active -= 1;
      if (request.command === "capture") throw new Error("synthetic failure with secret details");
      return ok(request, {});
    },
  };
  const controller = new FinanceInboundController("/tmp/workspace", runner);
  const [first, second] = await Promise.all([
    controller.handle(event, context),
    controller.handle(
      { ...event, messageId: "21" },
      { ...context, messageId: "21" },
    ),
  ]);
  assert.equal(maximum, 1);
  assert.deepEqual(first, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.deepEqual(second, first);
});

test("queued turn uses its admission deadline and never starts a late child", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run() {
      calls += 1;
      await gate;
      throw new Error("synthetic bounded refusal");
    },
  }, undefined, 20);
  const first = controller.handle(event, context);
  const second = controller.handle(
    { ...event, messageId: "21" },
    { ...context, messageId: "21" },
  );
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(await second, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.equal(calls, 1);
  release();
  await first;
});

test("controller refuses a ninth admitted turn before any additional child starts", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run() {
      calls += 1;
      await gate;
      throw new Error("synthetic bounded refusal");
    },
  });
  const admitted = Array.from({ length: 8 }, (_, index) => controller.handle(
    { ...event, messageId: String(20 + index) },
    { ...context, messageId: String(20 + index) },
  ));
  await new Promise<void>((resolve) => setImmediate(resolve));
  const refused = await controller.handle(
    { ...event, messageId: "28" },
    { ...context, messageId: "28" },
  );
  assert.deepEqual(refused, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.equal(calls, 1);
  release();
  await Promise.all(admitted);
  assert.equal(calls, 8);
});

test("review output refuses control characters and spoofed identities without active controls", async () => {
  const requests: BridgeRequest[] = [];
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      requests.push(request);
      if (request.command === "capture") {
        return ok(request, {
          intake_public_id: "raw_intake_bridge_686d5e5cd838efaa6565a084118bb81d",
        });
      }
      if (request.command === "propose") {
        return ok(request, {
          proposal_public_id: "prop_bridge_686d5e5cd838efaa6565a084118bb81d",
        });
      }
      if (request.command === "issue_human_actions") {
        return issuedActions(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d");
      }
      return reviewResult(request, "prop_bridge_686d5e5cd838efaa6565a084118bb81d", {
        parse_status: "pending\u0000\r\nspoofed",
        merchant: "taxi\nsecret-path" + "🙂".repeat(200),
      });
    },
  });
  const result = await controller.handle(event, context);
  assert.deepEqual(result, capturedFailure(
    "raw_intake_bridge_686d5e5cd838efaa6565a084118bb81d",
  ));
  assert.deepEqual(requests.map((request) => request.command), [
    "capture", "propose", "get_review", "get_ai_processing_status_v2",
  ]);

  const spoofed = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      if (request.command === "capture") {
        return ok(request, { intake_public_id: "raw_intake_spoofed" });
      }
      throw new Error("unreachable");
    },
  });
  assert.deepEqual(await spoofed.handle(event, context), {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
});

test("review refuses malformed account values before issuing active controls", async () => {
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  for (const [account, accountStatus] of [
    ["wallet\nspoofed", "present"],
    [7, "present"],
    ["   ", "present"],
    ["\u00a0", "present"],
    ["", "absent"],
    ["   ", "absent"],
  ] as const) {
    const commands: string[] = [];
    const controller = new FinanceInboundController("/tmp/workspace", {
      async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
        commands.push(request.command);
        if (request.command === "capture") {
          return ok(request, {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "propose") {
          return ok(request, { proposal_public_id: proposalPublicId });
        }
        return reviewResult(request, proposalPublicId, {
          account,
          account_status: accountStatus,
        });
      },
    });
    assert.deepEqual(await controller.handle(event, context), capturedFailure());
    assert.deepEqual(commands, ["capture", "propose", "get_review", "get_ai_processing_status_v2"]);
  }
});

test("review refuses malformed material fields and ambiguity indicators before controls", async () => {
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  for (const overrides of [
    { merchant: { name: "Cafe" } },
    { merchant: null, description: 7 },
    { amount: "   " },
    { currency: "\u00a0" },
    { transaction_date: "\t" },
    { merchant: "   ", description: "Lunch with client" },
    { merchant: null, description: "\u00a0" },
    { ambiguity_indicators: ["unknown_receipt_flag"] },
    { ambiguity_indicators: ["missing_amount", "missing_amount"] },
  ] as JsonObject[]) {
    const commands: string[] = [];
    const controller = new FinanceInboundController("/tmp/workspace", {
      async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
        commands.push(request.command);
        if (request.command === "capture") {
          return ok(request, {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "propose") {
          return ok(request, { proposal_public_id: proposalPublicId });
        }
        return reviewResult(request, proposalPublicId, overrides);
      },
    });
    assert.deepEqual(await controller.handle(event, context), capturedFailure());
    assert.deepEqual(commands, ["capture", "propose", "get_review", "get_ai_processing_status_v2"]);
  }
});

test("review refuses missing or mismatched AI provenance before issuing controls", async () => {
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  const cases: JsonObject[] = [
    { proposal_origin: "ai_fallback", ai_source_kind: null },
    { proposal_origin: "unknown_origin", ai_source_kind: null },
    { proposal_origin: "deterministic", ai_source_kind: "telegram_raw_text" },
    { proposal_origin: "ai_fallback", ai_source_kind: "receipt_local_ocr_text" },
    {
      proposal_origin: "ai_fallback",
      ai_source_kind: "telegram_raw_text",
      source_type: "telegram_image",
    },
  ];
  cases.push({ proposal_origin: "__omit__", ai_source_kind: null });

  for (const overrides of cases) {
    const commands: string[] = [];
    const controller = new FinanceInboundController("/tmp/workspace", {
      async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
        commands.push(request.command);
        if (request.command === "capture") {
          return ok(request, {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "propose") {
          return ok(request, { proposal_public_id: proposalPublicId });
        }
        const review = reviewResult(request, proposalPublicId, overrides);
        if (overrides.proposal_origin === "__omit__" && review.status === "ok") {
          delete review.result.proposal_origin;
        }
        return review;
      },
    });
    assert.deepEqual(await controller.handle(event, context), capturedFailure());
    assert.deepEqual(commands, ["capture", "propose", "get_review", "get_ai_processing_status_v2"]);
  }
});

test("review distinguishes literal material text from absent merchant and description", async () => {
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  const renderedCards: string[] = [];
  for (const [overrides, expected] of [
    [
      { merchant: null, description: "Lunch with client", confirm_available: false },
      ["Merchant: unset", 'Description: set "Lunch with client"'],
    ],
    [
      { merchant: "unspecified", description: "Lunch with client", confirm_available: false },
      ['Merchant: set "unspecified"', 'Description: set "Lunch with client"'],
    ],
    [
      { merchant: null, description: "not provided", confirm_available: false },
      ["Merchant: unset", 'Description: set "not provided"'],
    ],
    [
      {
        merchant: null, description: null, account: null, account_status: "absent",
        confirm_available: false,
      },
      ["Merchant: unset", "Description: unset", "Account: unset (unspecified)"],
    ],
    [
      {
        merchant: null,
        description: null,
        account: "unspecified",
        account_status: "present",
        confirm_available: false,
      },
      ["Merchant: unset", "Description: unset", 'Account: set "unspecified"'],
    ],
  ] as const) {
    const commands: string[] = [];
    const controller = new FinanceInboundController("/tmp/workspace", {
      async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
        commands.push(request.command);
        if (request.command === "capture") {
          return ok(request, {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "propose") {
          return ok(request, { proposal_public_id: proposalPublicId });
        }
        if (request.command === "issue_human_actions") {
          return issuedRejectAction(request, proposalPublicId);
        }
        return reviewResult(request, proposalPublicId, overrides);
      },
    });
    const result = await controller.handle(event, context);
    assert.equal(result.handled, true);
    renderedCards.push(replyText(result));
    for (const line of expected) assert.ok(replyText(result).includes(line));
    assert.deepEqual(commands, [
      "capture", "propose", "get_review", "get_ai_processing_status_v2", "issue_human_actions",
    ]);
  }
  assert.notEqual(renderedCards[0], renderedCards[1]);
  assert.notEqual(renderedCards[3], renderedCards[4]);
});

test("review refuses upstream truncation and issuance that is not bound to the rendered version", async () => {
  const proposalPublicId = "parser_output_12345678-1234-1234-1234-123456789abc";
  const commands: string[] = [];
  const truncated = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      commands.push(request.command);
      if (request.command === "capture") {
        return ok(request, { intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "propose") return ok(request, { proposal_public_id: proposalPublicId });
      return reviewResult(request, proposalPublicId, {
        merchant: null,
        description: "full description not represented",
        account: "A".repeat(1_024),
        account_status: "present",
        ambiguity_indicators: ["oversized_display_field"],
      });
    },
  });
  assert.deepEqual(await truncated.handle(event, context), capturedFailure());
  assert.deepEqual(commands, ["capture", "propose", "get_review", "get_ai_processing_status_v2"]);

  const mismatched = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      if (request.command === "capture") {
        return ok(request, { intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "propose") return ok(request, { proposal_public_id: proposalPublicId });
      if (request.command === "get_review") {
        return reviewResult(request, proposalPublicId, { merchant: null, description: "airport taxi" });
      }
      return ok(request, {
        proposal_public_id: proposalPublicId,
        proposal_version: 1,
        content_hash: "2".repeat(64),
        actions: {
          confirm: { reference: `fha1_${"A".repeat(24)}`, expiry: 2_000_000_000 },
          edit: { reference: `fha1_${"C".repeat(24)}`, expiry: 2_000_000_000 },
          reject: { reference: `fha1_${"B".repeat(24)}`, expiry: 2_000_000_000 },
        },
        final_transaction_created: false,
      });
    },
  });
  assert.deepEqual(await mismatched.handle(event, context), capturedFailure());
});

test("review preserves financial scalars exactly or refuses before presenting altered truth", async () => {
  const exactAmount = "1".repeat(161);
  const controller = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      if (request.command === "capture") {
        return ok(request, { intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "propose") {
        return ok(request, { proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "issue_human_actions") {
        return issuedRejectAction(
          request,
          "parser_output_12345678-1234-1234-1234-123456789abc",
        );
      }
      return reviewResult(request, "parser_output_12345678-1234-1234-1234-123456789abc", {
        amount: exactAmount,
        confirm_available: false,
      });
    },
  });
  const preserved = await controller.handle(event, context);
  assert.ok(replyText(preserved).includes(`Amount: set ${JSON.stringify(exactAmount)}`));
  assert.ok(replyText(preserved).includes('Currency: set "SGD"'));

  const unsafe = new FinanceInboundController("/tmp/workspace", {
    async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
      if (request.command === "capture") {
        return ok(request, { intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "propose") {
        return ok(request, { proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc" });
      }
      if (request.command === "issue_human_actions") {
        return issuedRejectAction(
          request,
          "parser_output_12345678-1234-1234-1234-123456789abc",
        );
      }
      return reviewResult(request, "parser_output_12345678-1234-1234-1234-123456789abc", {
        amount: "35.50\nspoofed",
        confirm_available: false,
      });
    },
  });
  assert.deepEqual(await unsafe.handle(event, context), capturedFailure());

  for (const unsafeScalar of ["12.34\ud800", "12.34\u0085spoofed"]) {
    const malformedUnicode = new FinanceInboundController("/tmp/workspace", {
      async run(request) {
      if (request.command === "get_guided_edit_session") return inactiveGuidedSession(request);
        if (request.command === "capture") {
          return ok(request, {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "propose") {
          return ok(request, {
            proposal_public_id: "parser_output_12345678-1234-1234-1234-123456789abc",
          });
        }
        if (request.command === "issue_human_actions") {
          return issuedRejectAction(
            request,
            "parser_output_12345678-1234-1234-1234-123456789abc",
          );
        }
        return reviewResult(request, "parser_output_12345678-1234-1234-1234-123456789abc", {
          amount: unsafeScalar,
          confirm_available: false,
        });
      },
    });
    assert.deepEqual(await malformedUnicode.handle(event, context), capturedFailure());
  }
});

test("oversized receipt caption is refused before media acquisition or handoff publication", async () => {
  await using fixture = await temporaryDirectory();
  const workspaceRoot = join(fixture.path, "workspace");
  await mkdir(workspaceRoot, { mode: 0o700 });
  await chmod(workspaceRoot, 0o700);
  let runnerCalls = 0;
  const controller = new FinanceInboundController(await realpath(workspaceRoot), {
    async run() {
      runnerCalls += 1;
      throw new Error("unreachable");
    },
  }, {
    media: new ReceiptMediaAdapter(() => join(fixture.path, "missing-media")),
    handoff: new HandoffPublisher(await realpath(workspaceRoot)),
  });
  const result = await controller.handle({
    ...event,
    content: "x".repeat(2_001),
    metadata: { ...event.metadata, mediaUrl: "media://inbound/receipt_123" },
  }, context);
  assert.deepEqual(result, {
    handled: true,
    reply: { text: "Finance intake could not be processed safely. Please retry." },
  });
  assert.equal(runnerCalls, 0);
  await assert.rejects(lstat(join(workspaceRoot, "handoff")), /ENOENT/u);
});
