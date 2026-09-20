import { createHash } from "node:crypto";
import { extname } from "node:path";

import type {
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
  PluginHookInboundClaimResult,
} from "openclaw-sdk/plugin-sdk/plugin-entry";

import {
  canonicalCaptureKey,
  captureIdentities,
  createHumanActionBatchId,
  createBridgeRequest,
  framedDigest,
  guidedEditCompleteKey,
  guidedEditUpdateKey,
  humanDraftApplyKey,
  humanDraftDeliveryAttemptId,
  humanDraftDeliveryKey,
  humanDraftObservationId,
  humanDraftObservationKey,
  humanDraftOperationId,
  humanDraftRecoveryId,
  humanDraftRecoveryKey,
  humanActionIssuanceKey,
  initialPostingReviewPreparationKey,
  postingActionIssuanceKey,
  postingReviewPreparationKey,
  type BridgeRequest,
  type BridgeResponse,
  type JsonObject,
  type JsonValue,
} from "./protocol.js";
import {
  humanActionCallbackData,
  type ActiveAction,
} from "./interactive.js";
import type { HandoffPublisher } from "./handoff.js";
import type {
  FinanceAgentConfigRefusalV2,
} from "./finance-agent-runtime-v2.js";
import type { FinanceAgentConfigProjectionV2 } from "./agent-profile-projection-v2.js";
import {
  parseProcessingStatusV2,
  renderProcessingFooterV2,
} from "./processing-status-v2.js";
import {
  ReceiptMediaUnavailableError,
  type ReceiptMediaAdapter,
  type ValidatedMedia,
} from "./media.js";
import {
  EMPTY_WHOLE_CARD_FIELDS,
  extractWholeCardReference,
  parseWholeCard,
  renderWholeCard,
  type WholeCardFields,
} from "./whole-card.js";

const COMMAND_DEADLINE_MS = 30_000;
const CONTROLLER_DEADLINE_MS = 105_000;
const MAX_TELEGRAM_TIMESTAMP_MS = 253_402_300_799_000;
const MAX_QUEUED_TURNS = 8;
const MAX_RECEIPT_CAPTION_CHARACTERS = 2_000;
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const RAW_INTAKE_ID = new RegExp(
  `^(?:raw_intake_bridge_[0-9a-f]{32}|raw_intake_${UUID})$`,
  "u",
);
const PROPOSAL_ID = new RegExp(
  `^(?:prop_bridge_[0-9a-f]{32}|parser_output_${UUID})$`,
  "u",
);
export const FINANCE_FAILURE_REPLY =
  "Finance intake could not be processed safely. Please retry.";
const REVIEW_AMBIGUITY_INDICATORS = new Set([
  "missing_amount",
  "missing_currency",
  "missing_date",
  "missing_merchant_or_description",
  "low_confidence",
  "ocr_no_text",
  "ocr_partial_text",
  "oversized_display_field",
  "unknown_classification",
  "total_not_found",
  "conflicting_total_candidates",
  "total_amount_invalid",
  "ambiguous_currency_symbol",
  "currency_not_determined",
  "unsupported_currency_for_amount",
  "ambiguous_transaction_date",
  "conflicting_date_candidates",
  "transaction_date_not_found",
  "merchant_not_determined",
  "ocr_unsupported_input",
  "ocr_engine_failed",
  "ocr_resource_rejected",
]);
const GUIDED_EDIT_FIELD_ALIASES = new Map<string, string>([
  ["amount", "amount"], ["金额", "amount"],
  ["currency", "currency"], ["币种", "currency"],
  ["transaction_date", "transaction_date"], ["date", "transaction_date"],
  ["日期", "transaction_date"],
  ["merchant", "merchant"], ["商户", "merchant"],
  ["description", "description"], ["描述", "description"],
  ["category", "category"], ["分类", "category"],
]);

function parseGuidedEditMessage(text: string):
  | { kind: "complete" }
  | { kind: "update"; field: string; value: string }
  | { kind: "invalid" } {
  const trimmed = text.trim();
  if (trimmed === "完成") return { kind: "complete" };
  const separator = trimmed.indexOf("=");
  if (separator <= 0 || separator !== trimmed.lastIndexOf("=")) return { kind: "invalid" };
  const alias = trimmed.slice(0, separator).trim().toLowerCase();
  const value = trimmed.slice(separator + 1).trim();
  const field = GUIDED_EDIT_FIELD_ALIASES.get(alias);
  if (field === undefined || value.length === 0 || Buffer.byteLength(value, "utf8") > 1_024 ||
      /[\p{C}\p{Zl}\p{Zp}]/u.test(value)) return { kind: "invalid" };
  return { kind: "update", field, value };
}

function looksLikeGuidedControl(text: string): boolean {
  const trimmed = text.trim();
  const separator = trimmed.indexOf("=");
  return trimmed === "完成" || (
    separator > 0 && separator === trimmed.lastIndexOf("=")
  );
}

export interface BridgeRunner {
  run(request: BridgeRequest, deadlineMs: number, inheritedFd?: number): Promise<BridgeResponse>;
}

interface ValidatedTextTurn {
  accountId: string;
  bindingId: string;
  chatId: number;
  messageId: number;
  senderId: number;
  sessionKey?: string;
  date: number;
  text: string;
  replyToId?: string;
  replyToIdFull?: string;
}

interface ReceiptDependencies {
  media: ReceiptMediaAdapter;
  handoff: HandoffPublisher;
}

type AiFallbackOutcome =
  | { kind: "unavailable" }
  | { kind: "proposal"; proposalPublicId: string }
  | { kind: "stopped"; reason: "classification_only" | "manual_recovery" };

export interface FinanceLlmCompletion {
  text: string;
  provider: string | null;
  model: string | null;
  agentId: string | null;
  usage: {
    inputTokens?: number;
    outputTokens?: number;
  };
  audit: {
    caller: {
      kind: "plugin" | "context-engine" | "host" | "unknown";
      id?: string | null;
      name?: string | null;
    };
    purpose?: string | null;
    sessionKey?: string;
  };
}

export interface FinanceLlmRuntime {
  currentProjection(): FinanceAgentConfigProjectionV2 | FinanceAgentConfigRefusalV2;
  complete(params: {
    messages: Array<{ role: "user"; content: string }>;
    model: string;
    maxTokens: number;
    temperature: 0;
    systemPrompt: string;
    purpose: string;
    agentId: string;
    maxRetries: 0;
    signal: AbortSignal;
  }): Promise<FinanceLlmCompletion>;
  recordStageTiming?(
    stage: "config_projection" | "receipt_lookup_and_bridge_prepare" |
      "host_completion" | "result_persistence",
    elapsedMs: number,
  ): void;
}

const AI_FALLBACK_AGENT_ID = "finance";
const AI_FALLBACK_PURPOSE = "finance-bridge.ai-proposal-v2";

function sessionKeyHash(value: string | undefined): string | null {
  if (value === undefined) return null;
  return createHash("sha256")
    .update("finance-ai-session-key-v1", "ascii")
    .update("\0", "ascii")
    .update(value, "utf8")
    .digest("hex");
}

function aiFallbackOutcome(result: JsonObject): AiFallbackOutcome {
  if (result.result_status === "proposal_created" &&
      typeof result.proposal_public_id === "string") {
    return { kind: "proposal", proposalPublicId: result.proposal_public_id };
  }
  return {
    kind: "stopped",
    reason: result.result_status === "classification_only"
      ? "classification_only"
      : "manual_recovery",
  };
}

function utf16Sha256(value: string): string {
  const codeUnits = Buffer.allocUnsafe(value.length * 2);
  for (let index = 0; index < value.length; index += 1) {
    codeUnits.writeUInt16BE(value.charCodeAt(index), index * 2);
  }
  const count = Buffer.allocUnsafe(8);
  count.writeBigUInt64BE(BigInt(value.length));
  return createHash("sha256")
    .update("finance-ai-js-utf16-v1", "ascii")
    .update("\0", "ascii")
    .update(count)
    .update(codeUnits)
    .digest("hex");
}

function metadataUtf16Sha256(field: string, value: string): string {
  const codeUnits = Buffer.allocUnsafe(value.length * 2);
  for (let index = 0; index < value.length; index += 1) {
    codeUnits.writeUInt16BE(value.charCodeAt(index), index * 2);
  }
  return createHash("sha256")
    .update("finance-ai-metadata-v1", "ascii")
    .update("\0", "ascii")
    .update(field, "ascii")
    .update("\0", "ascii")
    .update(codeUnits)
    .digest("hex");
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return true;
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function metadataIssue(completion: FinanceLlmCompletion): {
  field: string;
  reason: "invalid_type" | "oversize" | "unencodable" | "out_of_range";
  codeUnitCount: number | null;
  sha256: string | null;
} | undefined {
  const fields: Array<[string, unknown]> = [
    ["provider", completion.provider],
    ["model", completion.model],
    ["agent", completion.agentId],
    ["caller_kind", completion.audit?.caller?.kind],
    ["caller_id", completion.audit?.caller?.id],
    ["caller_name", completion.audit?.caller?.name],
    ["purpose", completion.audit?.purpose],
    ["session_key", completion.audit?.sessionKey],
  ];
  for (const [field, value] of fields) {
    if (value === undefined || value === null) continue;
    if (typeof value !== "string") {
      return { field, reason: "invalid_type", codeUnitCount: null, sha256: null };
    }
    if (hasUnpairedSurrogate(value)) {
      return {
        field,
        reason: "unencodable",
        codeUnitCount: value.length,
        sha256: value.length <= 4_096 ? metadataUtf16Sha256(field, value) : null,
      };
    }
    if (Buffer.byteLength(value, "utf8") > 256) {
      return {
        field,
        reason: "oversize",
        codeUnitCount: value.length,
        sha256: value.length <= 4_096 ? metadataUtf16Sha256(field, value) : null,
      };
    }
  }
  for (const [field, value] of [
    ["usage_input_tokens", completion.usage?.inputTokens],
    ["usage_output_tokens", completion.usage?.outputTokens],
  ] as const) {
    if (value !== undefined && value !== null &&
        (!Number.isSafeInteger(value) || value < 0 || value > 10_000_000)) {
      return { field, reason: "out_of_range", codeUnitCount: null, sha256: null };
    }
  }
  return undefined;
}

function requireFallbackModelCall(
  value: JsonValue | undefined,
  requestIdentity: JsonValue | undefined,
): {
  messages: Array<{ role: "user"; content: string }>;
  model: string;
  maxTokens: number;
  temperature: 0;
  systemPrompt: string;
  purpose: string;
  agentId: string;
} {
  if (!isJsonObject(requestIdentity) ||
      Object.keys(requestIdentity).sort().join(",") !==
        "agent_id,model,purpose,request_sha256" ||
      typeof requestIdentity.model !== "string" ||
      requestIdentity.agent_id !== AI_FALLBACK_AGENT_ID ||
      requestIdentity.purpose !== AI_FALLBACK_PURPOSE ||
      typeof requestIdentity.request_sha256 !== "string" ||
      !/^[0-9a-f]{64}$/u.test(requestIdentity.request_sha256)) {
    throw new Error("AI fallback request identity is invalid.");
  }
  if (!isJsonObject(value) ||
      Object.keys(value).sort().join(",") !==
        "agentId,maxTokens,messages,model,purpose,systemPrompt,temperature") {
    throw new Error("AI fallback model call contains unexpected fields.");
  }
  const messages = value.messages;
  if (!Array.isArray(messages) || messages.length !== 1 || !isJsonObject(messages[0]) ||
      Object.keys(messages[0]).sort().join(",") !== "content,role" ||
      messages[0].role !== "user" || typeof messages[0].content !== "string") {
    throw new Error("AI fallback model messages are not text-only.");
  }
  if (value.model !== requestIdentity.model || value.agentId !== AI_FALLBACK_AGENT_ID ||
      value.maxTokens !== 1024 || value.temperature !== 0 ||
      typeof value.systemPrompt !== "string" || value.purpose !== AI_FALLBACK_PURPOSE) {
    throw new Error("AI fallback model policy does not match the prepared request.");
  }
  return {
    messages: [{ role: "user", content: messages[0].content }],
    model: value.model,
    maxTokens: value.maxTokens,
    temperature: 0,
    systemPrompt: value.systemPrompt,
    purpose: value.purpose,
    agentId: value.agentId,
  };
}

function decimalInteger(value: string | undefined): number | undefined {
  if (value === undefined || !/^[1-9][0-9]*$/u.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

function bindingDataSender(value: unknown): string | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const sender = (value as Record<string, unknown>).senderId;
  return typeof sender === "string" ? sender : undefined;
}

function isTopLevelParent(parent: string | undefined, conversation: string | undefined): boolean {
  return parent === undefined || (conversation !== undefined && parent === conversation);
}

function validateTextTurn(
  event: PluginHookInboundClaimEvent,
  context: PluginHookInboundClaimContext,
): ValidatedTextTurn | undefined {
  const content = typeof event.content === "string" ? event.content : undefined;
  const hasReceipt = isReceiptMetadata(event.metadata);
  const binding = context.pluginBinding;
  if (binding === undefined || binding.pluginId !== "finance-bridge") return undefined;
  if (binding.channel !== "telegram" || event.channel !== "telegram" ||
      context.channelId !== "telegram" || event.isGroup !== false) {
    return undefined;
  }
  if (event.threadId !== undefined || binding.threadId !== undefined ||
      !isTopLevelParent(event.parentConversationId, event.conversationId) ||
      !isTopLevelParent(context.parentConversationId, context.conversationId) ||
      !isTopLevelParent(binding.parentConversationId, binding.conversationId)) {
    return undefined;
  }
  if (event.accountId === undefined || context.accountId === undefined ||
      event.accountId !== context.accountId || event.accountId !== binding.accountId) {
    return undefined;
  }
  if (event.conversationId === undefined || context.conversationId === undefined ||
      event.conversationId !== context.conversationId ||
      event.conversationId !== binding.conversationId) {
    return undefined;
  }
  const expectedSender = bindingDataSender(binding.data);
  if (event.senderId === undefined || context.senderId === undefined || expectedSender === undefined ||
      event.senderId !== context.senderId || event.senderId !== expectedSender) {
    return undefined;
  }
  if (event.messageId === undefined || context.messageId === undefined ||
      event.messageId !== context.messageId || event.senderIsOwner !== true ||
      event.commandAuthorized !== true || content === undefined ||
      content.trimStart().startsWith("/")) {
    return undefined;
  }
  const eventSessionKey = event.sessionKey;
  const contextSessionKey = context.sessionKey;
  if ((eventSessionKey !== undefined || contextSessionKey !== undefined) &&
      (eventSessionKey ?? contextSessionKey) !== (contextSessionKey ?? eventSessionKey)) {
    return undefined;
  }
  const sessionKey = contextSessionKey ?? eventSessionKey;
  if (sessionKey !== undefined &&
      (sessionKey.length === 0 || sessionKey.length > 200 || !/^[\x21-\x7e]+$/u.test(sessionKey))) {
    return undefined;
  }
  if ((event.replyToId !== undefined || context.replyToId !== undefined) &&
      (event.replyToId === undefined || context.replyToId === undefined ||
       event.replyToId !== context.replyToId)) {
    return undefined;
  }
  if ((event.replyToIdFull !== undefined || context.replyToIdFull !== undefined) &&
      (event.replyToIdFull === undefined || context.replyToIdFull === undefined ||
       event.replyToIdFull !== context.replyToIdFull)) {
    return undefined;
  }
  for (const replyIdentity of [event.replyToId, event.replyToIdFull]) {
    if (replyIdentity !== undefined &&
        (replyIdentity.length === 0 || replyIdentity.length > 200 ||
         !/^[\x21-\x7e]+$/u.test(replyIdentity))) {
      return undefined;
    }
  }
  const chatId = decimalInteger(event.conversationId);
  const senderId = decimalInteger(event.senderId);
  const messageId = decimalInteger(event.messageId);
  const timestampMs = event.timestamp;
  if (!Number.isSafeInteger(timestampMs) || (timestampMs as number) % 1_000 !== 0 ||
      (timestampMs as number) < 1_262_304_000_000 ||
      (timestampMs as number) > MAX_TELEGRAM_TIMESTAMP_MS ||
      chatId === undefined || senderId === undefined || messageId === undefined ||
      senderId !== chatId || (!hasReceipt && content.trim().length === 0)) {
    return undefined;
  }
  return {
    accountId: event.accountId,
    bindingId: binding.bindingId,
    chatId,
    messageId,
    senderId,
    ...(sessionKey === undefined ? {} : { sessionKey }),
    date: (timestampMs as number) / 1_000,
    text: content,
    ...(event.replyToId === undefined ? {} : { replyToId: event.replyToId }),
    ...(event.replyToIdFull === undefined ? {} : { replyToIdFull: event.replyToIdFull }),
  };
}

function requireOk(response: BridgeResponse): JsonObject {
  if (response.status !== "ok") {
    throw new Error(`Bridge command refused with ${response.error.code}.`);
  }
  return response.result;
}

function requirePublicId(object: JsonObject, field: string, kind: "intake" | "proposal"): string {
  const value = object[field];
  const pattern = kind === "intake" ? RAW_INTAKE_ID : PROPOSAL_ID;
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`Bridge result ${field} is invalid.`);
  }
  return value;
}

function renderReviewScalar(value: JsonValue | undefined, field: string): string {
  if (value === null || value === undefined || value === "") return "not provided";
  if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 1_024 ||
      /[\p{C}\p{Zl}\p{Zp}]/u.test(value)) {
    throw new Error(`Review ${field} cannot be represented without changing it.`);
  }
  return value;
}

function renderFinancialScalar(
  value: JsonValue | undefined,
  field: string,
): string | undefined {
  return renderOptionalReviewScalar(value, field);
}

function renderOptionalReviewScalar(
  value: JsonValue | undefined,
  field: string,
): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "string" && value.trim().length === 0) {
    throw new Error(`Review ${field} cannot be blank.`);
  }
  const rendered = renderReviewScalar(value, field);
  return rendered;
}

function renderBoundReviewLine(
  label: string,
  value: string | undefined,
  unset = "unset",
): string {
  return `${label}: ${value === undefined ? unset : `set ${JSON.stringify(value)}`}`;
}

function requireReviewBinding(review: JsonObject): {version: number; contentHash: string} {
  const version = review.proposal_version;
  const contentHash = review.effective_content_hash;
  if (typeof version !== "number" || !Number.isSafeInteger(version) || version < 0 ||
      typeof contentHash !== "string" || !/^[0-9a-f]{64}$/u.test(contentHash)) {
    throw new Error("Review version binding is invalid.");
  }
  return { version, contentHash };
}

function renderAmbiguities(value: JsonValue | undefined): string {
  if (!Array.isArray(value) || value.length > 32) {
    throw new Error("Review ambiguity indicators are invalid.");
  }
  const indicators = value.map((indicator) => {
    if (typeof indicator !== "string" || indicator.length === 0) {
      throw new Error("Review ambiguity indicator is invalid.");
    }
    return renderReviewScalar(indicator, "ambiguity");
  });
  if (new Set(indicators).size !== indicators.length ||
      indicators.some((indicator) => !REVIEW_AMBIGUITY_INDICATORS.has(indicator))) {
    throw new Error("Review ambiguity indicators are not recognized or unique.");
  }
  if (indicators.includes("oversized_display_field")) {
    throw new Error("Review contains hash-bound content that was truncated upstream.");
  }
  return indicators.length === 0 ? "none" : indicators.join(", ");
}

function receiptCaption(value: string): string | undefined {
  if (value.trim().length === 0) return undefined;
  let characters = 0;
  for (const character of value) {
    const codePoint = character.codePointAt(0)!;
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) {
      throw new Error("Receipt caption contains an invalid Unicode scalar.");
    }
    characters += 1;
  }
  if (characters > MAX_RECEIPT_CAPTION_CHARACTERS) {
    throw new Error("Receipt caption exceeds the durable source-text limit.");
  }
  return value;
}

function renderReview(review: JsonObject, confirmAvailable: boolean): string {
  const proposal = requirePublicId(review, "proposal_public_id", "proposal");
  const status = renderReviewScalar(review.parse_status, "parse_status");
  const merchant = renderOptionalReviewScalar(review.merchant, "merchant");
  const description = renderOptionalReviewScalar(review.description, "description");
  const amount = renderFinancialScalar(review.amount, "amount");
  const currency = renderFinancialScalar(review.currency, "currency");
  const date = renderFinancialScalar(review.transaction_date, "transaction_date");
  const account = renderOptionalReviewScalar(review.account, "account");
  const accountStatus = renderReviewScalar(review.account_status, "account_status");
  if (accountStatus !== "present" && accountStatus !== "absent") {
    throw new Error("Review account status is invalid.");
  }
  if ((accountStatus === "present") !== (account !== undefined)) {
    throw new Error("Review account value does not match its status.");
  }
  const classification = renderReviewScalar(review.classification, "classification");
  if (!["personal", "shared", "unknown"].includes(classification)) {
    throw new Error("Review classification is invalid.");
  }
  const sourceType = renderReviewScalar(review.source_type, "source_type");
  const proposalOrigin = renderReviewScalar(review.proposal_origin, "proposal_origin");
  const aiSourceKind = review.ai_source_kind === null
    ? null
    : renderReviewScalar(review.ai_source_kind, "ai_source_kind");
  const source = proposalOrigin === "deterministic" && aiSourceKind === null
    ? sourceType === "telegram_text"
      ? "text"
      : sourceType === "telegram_image"
        ? "receipt OCR"
        : undefined
    : proposalOrigin === "ai_fallback" &&
        sourceType === "telegram_text" && aiSourceKind === "telegram_raw_text"
      ? "AI-assisted text"
      : proposalOrigin === "ai_fallback" &&
          sourceType === "telegram_image" && aiSourceKind === "receipt_local_ocr_text"
        ? "AI-assisted local OCR"
        : undefined;
  if (source === undefined) throw new Error("Review source type is invalid.");
  const ambiguities = renderAmbiguities(review.ambiguity_indicators);
  const lines = [
    "Finance proposal ready for review.",
    `Proposal: ${proposal}`,
    `Status: ${status}`,
    renderBoundReviewLine("Merchant", merchant),
    renderBoundReviewLine("Description", description),
    renderBoundReviewLine("Amount", amount),
    renderBoundReviewLine("Currency", currency),
    renderBoundReviewLine("Date", date),
    renderBoundReviewLine("Account", account, "unset (unspecified)"),
    `Classification: ${classification}`,
    `Source: ${source}`,
    `Ambiguity: ${ambiguities}`,
    confirmAvailable
      ? "Confirm, edit, or reject below. Edits require explicit field=value messages."
      : "Reject below. The AI proposal remains unresolved and cannot be confirmed.",
  ];
  const rendered = lines.join("\n");
  if (Buffer.byteLength(rendered, "utf8") > 2_000) {
    throw new Error("Review card cannot represent the authoritative values without truncation.");
  }
  return rendered;
}

function requireActionReferences(
  result: JsonObject,
  confirmAvailable: boolean,
): Partial<Record<ActiveAction, string>> {
  const actions = result.actions;
  if (!isJsonObject(actions)) throw new Error("Human action references are missing.");
  const expected = confirmAvailable
    ? new Set(["confirm", "edit", "reject"])
    : new Set(["reject"]);
  if (Object.keys(actions).length !== expected.size ||
      Object.keys(actions).some((action) => !expected.has(action))) {
    throw new Error("Human action reference set is invalid.");
  }
  const output: Partial<Record<ActiveAction, string>> = {};
  for (const action of (confirmAvailable
    ? ["confirm", "edit", "reject"] as const
    : ["reject"] as const)) {
    const entry = actions[action];
    if (!isJsonObject(entry) || typeof entry.reference !== "string" ||
        typeof entry.expiry !== "number" || !Number.isSafeInteger(entry.expiry)) {
      throw new Error("Human action reference is invalid.");
    }
    if (!/^fha1_[A-Za-z0-9_-]{24}$/u.test(entry.reference)) {
      throw new Error("Human action reference is invalid.");
    }
    humanActionCallbackData(action, entry.reference);
    output[action] = entry.reference;
  }
  return output;
}

interface ValidatedPostingReview {
  reviewPublicId: string;
  postingPath: "text" | "personal_receipt";
  text: string;
}

function requireInitialPostingReview(
  result: JsonObject,
  proposalPublicId: string,
  proposalReview: JsonObject,
  reviewBinding: {version: number; contentHash: string},
): ValidatedPostingReview {
  const reviewPublicId = result.review_public_id;
  const initialCardPublicId = result.initial_card_public_id;
  const projection = result.visible_projection;
  const postingPath = result.posting_path;
  if (typeof reviewPublicId !== "string" || !/^d2rev_[0-9a-f]{30}$/u.test(reviewPublicId) ||
      typeof initialCardPublicId !== "string" || !/^d2card_[0-9a-f]{32}$/u.test(initialCardPublicId) ||
      result.card_generation_public_id !== null || result.proposal_public_id !== proposalPublicId ||
      result.proposal_version !== reviewBinding.version ||
      result.proposal_content_hash !== reviewBinding.contentHash ||
      (postingPath !== "text" && postingPath !== "personal_receipt") ||
      typeof result.visible_projection_hash !== "string" ||
      !/^[0-9a-f]{64}$/u.test(result.visible_projection_hash) ||
      typeof result.expires_at !== "number" || !Number.isSafeInteger(result.expires_at) ||
      result.final_transaction_created !== false || !isJsonObject(projection) ||
      typeof result.presentation_text !== "string") {
    throw new Error("D2 initial posting review identity is invalid.");
  }
  const expected = {
    amount: proposalReview.amount,
    currency: proposalReview.currency,
    transaction_date: proposalReview.transaction_date,
    merchant: proposalReview.merchant,
    account: "unspecified",
  };
  for (const [field, value] of Object.entries(expected)) {
    if (projection[field] !== value) {
      throw new Error(`D2 initial visible projection ${field} differs from the proposal review.`);
    }
  }
  const lines = [
    `Card Ref: ${initialCardPublicId}`,
    `Amount: ${projection.amount}`,
    `Currency: ${projection.currency}`,
    `Date: ${projection.transaction_date}`,
    `Merchant: ${projection.merchant ?? "Not specified"}`,
    `Description: ${proposalReview.description ?? "Not specified"}`,
    `Category: ${proposalReview.category ?? "Not specified"}`,
    "Account: Not specified",
  ];
  if (postingPath === "personal_receipt") {
    const calculation = projection.calculation;
    if (projection.receipt_total !== proposalReview.amount ||
        projection.personal_share !== proposalReview.amount || !isJsonObject(calculation) ||
        calculation.total_paid !== proposalReview.amount ||
        typeof calculation.total_to_collect !== "string" ||
        !Array.isArray(calculation.settlement_obligations) ||
        calculation.settlement_obligations.length !== 0) {
      throw new Error("D2 initial personal receipt projection is invalid.");
    }
    lines.push(
      "Source: Receipt",
      `Receipt total: ${projection.receipt_total}`,
      "Posting basis: one receipt-total line",
      `Your share: ${projection.personal_share}`,
      `Collectible from others: ${calculation.total_to_collect}`,
      "Settlement obligations: none",
      "No itemization, tax, fee, or shared allocation will be inferred.",
    );
  } else {
    lines.push("No account or shared-expense details will be inferred.");
  }
  const text = lines.join("\n");
  if (result.presentation_text !== text || Buffer.byteLength(text, "utf8") > 4_000) {
    throw new Error("D2 initial presentation differs from the authoritative review.");
  }
  return { reviewPublicId, postingPath, text };
}

function requirePostingReview(
  result: JsonObject,
  card: ValidatedHumanDraftCard,
): ValidatedPostingReview {
  const reviewPublicId = result.review_public_id;
  const projection = result.visible_projection;
  const postingPath = result.posting_path;
  if (typeof reviewPublicId !== "string" || !/^d2rev_[0-9a-f]{30}$/u.test(reviewPublicId) ||
      result.card_generation_public_id !== card.cardReference ||
      result.proposal_public_id !== card.proposalPublicId ||
      result.proposal_version !== card.proposalVersion ||
      result.proposal_content_hash !== card.proposalContentHash ||
      (postingPath !== "text" && postingPath !== "personal_receipt") ||
      typeof result.visible_projection_hash !== "string" ||
      !/^[0-9a-f]{64}$/u.test(result.visible_projection_hash) ||
      typeof result.expires_at !== "number" || !Number.isSafeInteger(result.expires_at) ||
      result.final_transaction_created !== false || !isJsonObject(projection)) {
    throw new Error("D2 posting review identity is invalid.");
  }
  const expected = {
    amount: card.fields.amount,
    currency: card.fields.currency,
    transaction_date: card.fields.transaction_date,
    merchant: card.fields.merchant,
    account: "unspecified",
  };
  for (const [field, value] of Object.entries(expected)) {
    if (projection[field] !== value) {
      throw new Error(`D2 visible projection ${field} differs from the current card.`);
    }
  }
  const lines = [
    `Card Ref: ${card.cardReference}`,
    `Amount: ${projection.amount}`,
    `Currency: ${projection.currency}`,
    `Date: ${projection.transaction_date}`,
    `Merchant: ${projection.merchant ?? "Not specified"}`,
    `Description: ${card.fields.description || "Not specified"}`,
    `Category: ${card.fields.category || "Not specified"}`,
    "Account: Not specified",
  ];
  if (postingPath === "personal_receipt") {
    const calculation = projection.calculation;
    if (projection.receipt_total !== card.fields.amount ||
        projection.personal_share !== card.fields.amount || !isJsonObject(calculation) ||
        calculation.total_paid !== card.fields.amount ||
        typeof calculation.total_to_collect !== "string" ||
        !Array.isArray(calculation.settlement_obligations) ||
        calculation.settlement_obligations.length !== 0) {
      throw new Error("D2 personal receipt projection is invalid.");
    }
    lines.push(
      "Source: Receipt",
      `Receipt total: ${projection.receipt_total}`,
      "Posting basis: one receipt-total line",
      `Your share: ${projection.personal_share}`,
      `Collectible from others: ${calculation.total_to_collect}`,
      "Settlement obligations: none",
      "No itemization, tax, fee, or shared allocation will be inferred.",
    );
  } else if (Object.keys(projection).some((field) =>
    ["receipt_total", "personal_share", "calculation"].includes(field))) {
    throw new Error("D2 text projection contains receipt-only fields.");
  } else {
    lines.push("No account or shared-expense details will be inferred.");
  }
  const text = lines.join("\n");
  if (Buffer.byteLength(text, "utf8") > 4_000) {
    throw new Error("D2 review card cannot be represented without truncation.");
  }
  return { reviewPublicId, postingPath, text };
}

interface PostingDeliveryManifest {
  attemptNonce: string;
  buttons: Array<Array<{text: string; callback_data: string}>>;
  text: string;
}

function requirePostingDeliveryManifest(
  result: JsonObject,
  review: ValidatedPostingReview,
): PostingDeliveryManifest {
  const controls = result.controls;
  if (result.posting_review_public_id !== review.reviewPublicId ||
      typeof result.delivery_attempt_public_id !== "string" ||
      !/^d2send_[0-9a-f]{32}$/u.test(result.delivery_attempt_public_id) ||
      result.delivery_manifest_version !== "finance_d2_controls_v1" ||
      result.text !== review.text ||
      typeof result.finance_delivery_material_sha256 !== "string" ||
      !/^[0-9a-f]{64}$/u.test(result.finance_delivery_material_sha256) ||
      typeof result.delivery_attempt_nonce !== "string" ||
      !/^d2nonce_[0-9a-f]{32}$/u.test(result.delivery_attempt_nonce) ||
      result.final_transaction_created !== false || !Array.isArray(controls) ||
      controls.length !== 3) {
    throw new Error("D2 delivery manifest is invalid.");
  }
  const expected = [
    { action: "confirm", label: "Confirm", row: 0, column: 0, route: "post:" },
    { action: "edit", label: "Edit", row: 1, column: 0, route: "edit:" },
    { action: "reject", label: "Reject", row: 1, column: 1, route: "reject:" },
  ] as const;
  const buttons: PostingDeliveryManifest["buttons"] = [[], []];
  for (let index = 0; index < expected.length; index += 1) {
    const control = controls[index];
    const contract = expected[index]!;
    if (!isJsonObject(control) || control.action !== contract.action ||
        control.label !== contract.label || control.row_index !== contract.row ||
        control.column_index !== contract.column ||
        typeof control.callback_value !== "string" ||
        !control.callback_value.startsWith(contract.route)) {
      throw new Error("D2 delivery control is invalid.");
    }
    const reference = control.callback_value.slice(contract.route.length);
    if (!/^fha1_[A-Za-z0-9_-]{24}$/u.test(reference)) {
      throw new Error("D2 delivery control reference is invalid.");
    }
    if (control.callback_value !== `${contract.route}${reference}`) {
      throw new Error("D2 delivery callback route is invalid.");
    }
    buttons[contract.row]!.push({
      text: contract.label,
      callback_data: control.callback_value,
    });
  }
  return {
    attemptNonce: result.delivery_attempt_nonce,
    buttons,
    text: result.text,
  };
}

function isJsonObject(value: JsonValue | undefined): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

interface ValidatedHumanDraftCard {
  draftPublicId: string;
  cardReference: string;
  currentCardReference: string;
  fields: WholeCardFields;
  completeness: "complete" | "incomplete";
  unresolvedFlags: string[];
  actionIssueBatchId: string;
  proposalPublicId: string;
  proposalVersion: number;
  proposalContentHash: string;
  confirmAvailable: boolean;
  rejectAvailable: boolean;
  originalOperationOrStartPublicId: string;
  deliveryStateHash: string;
  deliveryState: string;
  idempotentReplay: boolean;
  operationOutcome: string;
  refusalCode: string | null;
}

const D1_PROPOSAL_ID = new RegExp(
  `^(?:po_d1_[0-9a-f]{32}|prop_bridge_[0-9a-f]{32}|parser_output_${UUID})$`,
  "u",
);
const D1_CARD_ID = /^d1card_[0-9a-f]{32}$/u;
const HUMAN_DRAFT_RESULT_FIELDS = new Set([
  "draft_public_id", "draft_version", "draft_content_hash", "completeness",
  "reason_contributors", "unresolved_flags", "human_reply_evidence_public_id",
  "delivery_state", "delivery_state_hash", "delivery_attempts", "delivery_outcomes",
  "action_issue_batch_id", "operation_outcome", "refusal_code", "idempotent_replay",
  "action_issuance_state", "proposal_public_id", "proposal_version",
  "proposal_content_hash", "card_generation_public_id",
  "current_card_generation_public_id", "original_operation_or_start_public_id",
  "field_values", "decision_target_proposal_public_id",
  "decision_target_proposal_version", "decision_target_proposal_content_hash",
  "confirm_available", "reject_available", "final_transaction_created",
]);

function requireExactResultFields(result: JsonObject, expected: Set<string>): void {
  const keys = Object.keys(result);
  if (keys.length !== expected.size || keys.some((key) => !expected.has(key))) {
    throw new Error("Human draft result fields are invalid.");
  }
}

function requireNonNegativeInteger(value: JsonValue | undefined, field: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`Human draft ${field} is invalid.`);
  }
  return value;
}

function requireSha256(value: JsonValue | undefined, field: string): string {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/u.test(value)) {
    throw new Error(`Human draft ${field} is invalid.`);
  }
  return value;
}

function requireD1Proposal(value: JsonValue | undefined, field: string): string {
  if (typeof value !== "string" || !D1_PROPOSAL_ID.test(value)) {
    throw new Error(`Human draft ${field} is invalid.`);
  }
  return value;
}

function requireHumanDraftCard(result: JsonObject): ValidatedHumanDraftCard {
  requireExactResultFields(result, HUMAN_DRAFT_RESULT_FIELDS);
  if (typeof result.draft_public_id !== "string" ||
      !/^d1draft_[0-9a-f]{32}$/u.test(result.draft_public_id) ||
      typeof result.card_generation_public_id !== "string" ||
      !D1_CARD_ID.test(result.card_generation_public_id) ||
      typeof result.current_card_generation_public_id !== "string" ||
      !D1_CARD_ID.test(result.current_card_generation_public_id) ||
      typeof result.original_operation_or_start_public_id !== "string" ||
      !/^(?:d1op|d1start)_[0-9a-f]{32}$/u.test(result.original_operation_or_start_public_id) ||
      result.final_transaction_created !== false ||
      typeof result.idempotent_replay !== "boolean" ||
      !["started", "accepted", "refused", "noop", "confirmed", "rejected"].includes(
        String(result.operation_outcome),
      ) ||
      !["not_issued", "issued"].includes(String(result.action_issuance_state)) ||
      !["not_attempted", "unknown", "success", "failure"].includes(
        String(result.delivery_state),
      )) {
    throw new Error("Human draft identity or state is invalid.");
  }
  requireNonNegativeInteger(result.draft_version, "draft_version");
  requireSha256(result.draft_content_hash, "draft_content_hash");
  const deliveryStateHash = requireSha256(result.delivery_state_hash, "delivery_state_hash");
  if (!Array.isArray(result.reason_contributors) || result.reason_contributors.length > 64 ||
      !result.reason_contributors.every((item) => isJsonObject(item)) ||
      !Array.isArray(result.delivery_attempts) || result.delivery_attempts.length > 8 ||
      !result.delivery_attempts.every((item) => isJsonObject(item)) ||
      !Array.isArray(result.delivery_outcomes) || result.delivery_outcomes.length > 16 ||
      !result.delivery_outcomes.every((item) => isJsonObject(item))) {
    throw new Error("Human draft evidence or delivery history is invalid.");
  }
  if (!Array.isArray(result.unresolved_flags) || result.unresolved_flags.length > 32 ||
      !result.unresolved_flags.every((flag) => typeof flag === "string" &&
        /^[a-z0-9_]{1,100}$/u.test(flag)) ||
      new Set(result.unresolved_flags).size !== result.unresolved_flags.length) {
    throw new Error("Human draft unresolved flags are invalid.");
  }
  if (!isJsonObject(result.field_values) ||
      Object.keys(result.field_values).sort().join(",") !==
        "amount,category,currency,description,merchant,transaction_date" ||
      !Object.values(result.field_values).every((value) => typeof value === "string")) {
    throw new Error("Human draft field values are invalid.");
  }
  const fields = result.field_values as WholeCardFields;
  const completeness = result.completeness;
  if (completeness !== "complete" && completeness !== "incomplete") {
    throw new Error("Human draft completeness is invalid.");
  }
  if (typeof result.action_issue_batch_id !== "string" ||
      !/^[0-9a-f]{64}$/u.test(result.action_issue_batch_id) ||
      typeof result.confirm_available !== "boolean" ||
      typeof result.reject_available !== "boolean" ||
      (result.confirm_available &&
       (completeness !== "complete" || result.reject_available !== true ||
        result.card_generation_public_id !== result.current_card_generation_public_id))) {
    throw new Error("Human draft action availability is invalid.");
  }
  if (result.refusal_code !== null &&
      (typeof result.refusal_code !== "string" || !/^[A-Z0-9_]{1,100}$/u.test(result.refusal_code))) {
    throw new Error("Human draft refusal is invalid.");
  }
  if ((result.operation_outcome === "refused") !== (result.refusal_code !== null)) {
    throw new Error("Human draft refusal outcome is inconsistent.");
  }
  if (result.human_reply_evidence_public_id !== null &&
      (typeof result.human_reply_evidence_public_id !== "string" ||
       !/^d1evidence_[0-9a-f]{32}$/u.test(result.human_reply_evidence_public_id))) {
    throw new Error("Human draft evidence identity is invalid.");
  }
  const decisionTarget = requireD1Proposal(
    result.decision_target_proposal_public_id,
    "decision_target_proposal_public_id",
  );
  const decisionTargetVersion = requireNonNegativeInteger(
    result.decision_target_proposal_version,
    "decision_target_proposal_version",
  );
  const decisionTargetHash = requireSha256(
    result.decision_target_proposal_content_hash,
    "decision_target_proposal_content_hash",
  );
  let proposalPublicId = decisionTarget;
  let proposalVersion = decisionTargetVersion;
  let proposalContentHash = decisionTargetHash;
  if (completeness === "complete") {
    proposalPublicId = requireD1Proposal(result.proposal_public_id, "proposal_public_id");
    proposalVersion = requireNonNegativeInteger(result.proposal_version, "proposal_version");
    proposalContentHash = requireSha256(result.proposal_content_hash, "proposal_content_hash");
  } else if (result.proposal_public_id !== null || result.proposal_version !== null ||
      result.proposal_content_hash !== null) {
    throw new Error("Incomplete human draft unexpectedly exposes a publication.");
  }
  renderWholeCard({
    cardReference: result.card_generation_public_id,
    fields,
    language: "en",
    status: completeness === "complete" ? "publishable" : "incomplete",
    unresolvedReasons: result.unresolved_flags as string[],
  });
  return {
    draftPublicId: result.draft_public_id,
    cardReference: result.card_generation_public_id,
    currentCardReference: result.current_card_generation_public_id,
    fields,
    completeness,
    unresolvedFlags: result.unresolved_flags as string[],
    actionIssueBatchId: result.action_issue_batch_id,
    proposalPublicId,
    proposalVersion,
    proposalContentHash,
    confirmAvailable: result.confirm_available,
    rejectAvailable: result.reject_available,
    originalOperationOrStartPublicId: result.original_operation_or_start_public_id,
    deliveryStateHash,
    deliveryState: result.delivery_state as string,
    idempotentReplay: result.idempotent_replay,
    operationOutcome: result.operation_outcome as string,
    refusalCode: result.refusal_code as string | null,
  };
}

function requireActionableHumanDraftCard(result: JsonObject): ValidatedHumanDraftCard {
  const card = requireHumanDraftCard(result);
  if (card.operationOutcome === "refused") {
    throw new Error("Refused D1 result is not actionable.");
  }
  return card;
}

function looksLikeWholeCard(text: string): boolean {
  return /^(?:[ \t]*(?:资料卡编号|金额|币种|日期|商户|描述|分类|card[ \t]+ref|amount|currency|date|merchant|description|category)[ \t]*[:：])/imu
    .test(text);
}

export class FinanceInboundController {
  private queue: Promise<void> = Promise.resolve();
  private queuedTurns = 0;

  constructor(
    private readonly workspaceRoot: string,
    private readonly runner: BridgeRunner,
    private readonly receipt?: ReceiptDependencies,
    private readonly controllerDeadlineMs = CONTROLLER_DEADLINE_MS,
    private readonly llmRuntime?: FinanceLlmRuntime,
  ) {}

  async handle(
    event: PluginHookInboundClaimEvent,
    context: PluginHookInboundClaimContext,
  ): Promise<PluginHookInboundClaimResult> {
    const turn = validateTextTurn(event, context);
    if (turn === undefined) return { handled: true };
    if (this.queuedTurns >= MAX_QUEUED_TURNS) {
      return { handled: true, reply: { text: FINANCE_FAILURE_REPLY } };
    }

    const admittedAt = performance.now();
    this.queuedTurns += 1;
    let release!: () => void;
    const predecessor = this.queue;
    const completion = new Promise<void>((resolve) => { release = resolve; });
    this.queue = predecessor.then(async () => await completion);
    let queueTimer: NodeJS.Timeout | undefined;
    try {
      if (!Number.isSafeInteger(this.controllerDeadlineMs) || this.controllerDeadlineMs <= 0 ||
          this.controllerDeadlineMs > CONTROLLER_DEADLINE_MS) {
        throw new Error("Finance controller deadline is invalid.");
      }
      await Promise.race([
        predecessor,
        new Promise<never>((_resolve, reject) => {
          queueTimer = setTimeout(
            () => reject(new Error("Finance controller queue deadline exceeded.")),
            this.controllerDeadlineMs,
          );
        }),
      ]);
      const remaining = Math.floor(this.controllerDeadlineMs - (performance.now() - admittedAt));
      if (remaining <= 0) throw new Error("Finance controller queue deadline exceeded.");
      const hasMedia = isReceiptMetadata(event.metadata);
      if (hasMedia) receiptCaption(turn.text);
      const wholeCard = await this.runWholeCardTurn(turn, hasMedia, remaining);
      if (wholeCard !== undefined) return wholeCard;
      const guided = await this.runGuidedEditTurn(turn, hasMedia, remaining);
      if (guided !== undefined) return guided;
      return hasMedia
        ? await this.runReceiptTurn(turn, event.metadata!, remaining)
        : await this.runTextTurn(turn, remaining);
    } catch {
      return { handled: true, reply: { text: FINANCE_FAILURE_REPLY } };
    } finally {
      if (queueTimer !== undefined) clearTimeout(queueTimer);
      this.queuedTurns -= 1;
      release();
    }
  }

  private async runWholeCardTurn(
    turn: ValidatedTextTurn,
    hasMedia: boolean,
    admittedDeadlineMs: number,
  ): Promise<PluginHookInboundClaimResult | undefined> {
    const extractedReference = extractWholeCardReference(turn.text);
    const cardLike = extractedReference !== undefined || looksLikeWholeCard(turn.text);
    if (!cardLike) return undefined;
    if (hasMedia) {
      return {
        handled: true,
        reply: { text: "Receipt media cannot be attached to a Finance whole-card edit. Resend the text card only." },
      };
    }
    let cardReference: string;
    let fields: WholeCardFields;
    try {
      const parsed = parseWholeCard(turn.text);
      cardReference = parsed.cardReference;
      fields = parsed.fields;
    } catch {
      if (extractedReference === undefined) {
        return {
          handled: true,
          reply: {
            text: "Finance card edit was refused. Use the complete card and keep its Card Ref unchanged.",
          },
        };
      }
      cardReference = extractedReference;
      fields = { ...EMPTY_WHOLE_CARD_FIELDS };
    }

    const startedAt = performance.now();
    const deadline = (): number => {
      const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
      if (remaining <= 0) throw new Error("Finance controller deadline exceeded.");
      return Math.min(COMMAND_DEADLINE_MS, remaining);
    };
    const commandContext = {
      workspace_path: this.workspaceRoot,
      operator_actor_id: String(turn.senderId),
      telegram_account_id: turn.accountId,
      telegram_conversation_id: String(turn.chatId),
      conversation_binding_id: turn.bindingId,
    };
    const operationPublicId = humanDraftOperationId(
      turn.accountId,
      String(turn.chatId),
      turn.bindingId,
      turn.messageId,
      cardReference,
    );
    const appliedResponse = await this.runner.run(createBridgeRequest(
      "apply_human_draft_card",
      {
        ...commandContext,
        card_generation_public_id: cardReference,
        telegram_message_id: turn.messageId,
        operation_public_id: operationPublicId,
        raw_card_text: turn.text,
        field_values: fields,
      },
      humanDraftApplyKey(operationPublicId),
    ), deadline());
    if (appliedResponse.status !== "ok") {
      return {
        handled: true,
        reply: { text: "Finance card edit was refused. The previous authoritative card remains current." },
      };
    }
    let card = requireHumanDraftCard(appliedResponse.result);
    if (card.operationOutcome === "refused") {
      return {
        handled: true,
        reply: {
          text: "Finance card edit was refused. The previous authoritative card remains current.",
        },
      };
    }
    if (card.cardReference !== card.currentCardReference) {
      const current = requireActionableHumanDraftCard(requireOk(await this.runner.run(createBridgeRequest(
        "get_human_draft_card",
        { ...commandContext, card_generation_public_id: card.currentCardReference },
      ), deadline())));
      if (current.cardReference !== card.currentCardReference ||
          current.currentCardReference !== card.currentCardReference ||
          current.draftPublicId !== card.draftPublicId ||
          current.originalOperationOrStartPublicId !== card.originalOperationOrStartPublicId) {
        throw new Error("D1 current-generation query did not return the authoritative winner.");
      }
      card = current;
    }
    if (card.idempotentReplay && card.deliveryState === "unknown") {
      const queried = requireActionableHumanDraftCard(requireOk(await this.runner.run(createBridgeRequest(
        "get_human_draft_card",
        { ...commandContext, operation_public_id: operationPublicId },
      ), deadline())));
      if (queried.cardReference !== card.cardReference ||
          queried.currentCardReference !== card.cardReference ||
          queried.originalOperationOrStartPublicId !== card.originalOperationOrStartPublicId ||
          queried.deliveryState !== "unknown") {
        throw new Error("D1 unknown delivery query did not return the exact card.");
      }
      const recoveryPublicId = humanDraftRecoveryId(
        queried.draftPublicId,
        queried.originalOperationOrStartPublicId,
        queried.cardReference,
      );
      const reissued = requireActionableHumanDraftCard(requireOk(await this.runner.run(createBridgeRequest(
        "reissue_human_draft_card",
        {
          ...commandContext,
          expected_current_generation_public_id: queried.cardReference,
          original_operation_or_start_public_id: queried.originalOperationOrStartPublicId,
          recovery_public_id: recoveryPublicId,
          recovery_material_hash: framedDigest(
            "d1-card-recovery-material-v1",
            recoveryPublicId,
            queried.deliveryStateHash,
          ),
          queried_delivery_state_hash: queried.deliveryStateHash,
          reason: "unknown_after_query",
        },
        humanDraftRecoveryKey(recoveryPublicId),
      ), deadline())));
      if (reissued.cardReference === queried.cardReference ||
          reissued.currentCardReference !== reissued.cardReference ||
          reissued.draftPublicId !== queried.draftPublicId ||
          reissued.originalOperationOrStartPublicId !== queried.originalOperationOrStartPublicId) {
        throw new Error("D1 reissue did not return the authoritative successor card.");
      }
      card = reissued;
    }
    if (!card.rejectAvailable) {
      return {
        handled: true,
        reply: { text: "Finance card is no longer active. Request the current Finance record." },
      };
    }
    const language = /(?:资料卡编号|金额|币种|日期|商户|描述|分类)/u.test(turn.text)
      ? "zh" as const
      : "en" as const;
    let text = renderWholeCard({
      cardReference: card.cardReference,
      fields: card.fields,
      language,
      status: card.completeness === "complete" ? "publishable" : "incomplete",
      unresolvedReasons: card.unresolvedFlags,
    });
    if (card.confirmAvailable) {
      return await this.deliverHumanDraftPostingReview(card, turn, deadline);
    }
    const issuedResponse = await this.runner.run(createBridgeRequest(
      "issue_human_actions",
      {
        ...commandContext,
        proposal_public_id: card.proposalPublicId,
        operator_actor_id: String(turn.senderId),
        reference_batch_id: card.actionIssueBatchId,
        token_ttl_seconds: 600,
        expected_proposal_version: card.proposalVersion,
        expected_content_hash: card.proposalContentHash,
        card_generation_public_id: card.cardReference,
      },
      humanActionIssuanceKey(card.actionIssueBatchId),
    ), deadline());
    const issued = requireOk(issuedResponse);
    if (issued.proposal_public_id !== card.proposalPublicId ||
        issued.proposal_version !== card.proposalVersion ||
        issued.content_hash !== card.proposalContentHash ||
        issued.card_generation_public_id !== card.cardReference ||
        issued.final_transaction_created !== false) {
      throw new Error("D1 human action issuance identity mismatch.");
    }
    const references = requireActionReferences(issued, false);
    if (references.reject === undefined) throw new Error("D1 Reject reference is missing.");
    const buttons = [];
    buttons.push({
      label: "Reject",
      style: "danger" as const,
      action: { type: "callback" as const, value: humanActionCallbackData("reject", references.reject) },
    });

    const attemptPublicId = humanDraftDeliveryAttemptId(card.cardReference, "reply");
    const deliveryMaterialHash = framedDigest(
      "d1-card-delivery-material-v1",
      text,
      ...buttons.map((button) => button.action.value),
    );
    const begin = requireOk(await this.runner.run(createBridgeRequest(
      "begin_human_draft_card_delivery",
      {
        ...commandContext,
        card_generation_public_id: card.cardReference,
        attempt_public_id: attemptPublicId,
        delivery_material_hash: deliveryMaterialHash,
        transport_mode: "reply",
        ...(turn.replyToId === undefined ? {} : { outbound_target_message_id: turn.replyToId }),
      },
      humanDraftDeliveryKey(attemptPublicId),
    ), deadline()));
    if (begin.attempt_public_id !== attemptPublicId || Object.keys(begin).length !== 1) {
      throw new Error("D1 delivery attempt identity mismatch.");
    }
    const observationPublicId = humanDraftObservationId(attemptPublicId, "initial");
    const observed = requireOk(await this.runner.run(createBridgeRequest(
      "record_human_draft_card_delivery_outcome",
      {
        ...commandContext,
        attempt_public_id: attemptPublicId,
        observation_public_id: observationPublicId,
        outcome: "unknown",
        error_code: null,
        outbound_message_id: null,
        trusted_receipt_hash: null,
      },
      humanDraftObservationKey(observationPublicId),
    ), deadline()));
    if (observed.observation_public_id !== observationPublicId || Object.keys(observed).length !== 1) {
      throw new Error("D1 delivery observation identity mismatch.");
    }
    return {
      handled: true,
      reply: {
        presentation: {
          blocks: [
            { type: "text", text },
            { type: "buttons", buttons },
          ],
        },
      },
    };
  }

  private async runGuidedEditTurn(
    turn: ValidatedTextTurn,
    hasMedia: boolean,
    admittedDeadlineMs: number,
  ): Promise<PluginHookInboundClaimResult | undefined> {
    const parsed = parseGuidedEditMessage(turn.text);
    const startedAt = performance.now();
    const deadline = (): number => {
      const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
      if (remaining <= 0) throw new Error("Finance controller deadline exceeded.");
      return Math.min(COMMAND_DEADLINE_MS, remaining);
    };
    const context = {
      workspace_path: this.workspaceRoot,
      operator_actor_id: String(turn.senderId),
      telegram_account_id: turn.accountId,
      telegram_conversation_id: String(turn.chatId),
      conversation_binding_id: turn.bindingId,
    };
    const lookup = requireOk(await this.runner.run(createBridgeRequest(
      "get_guided_edit_session", { ...context, telegram_message_id: turn.messageId },
    ), deadline()));
    if (lookup.active === false && lookup.session_status === "inactive" &&
        lookup.final_transaction_created === false) {
      if (parsed.kind !== "invalid" || looksLikeGuidedControl(turn.text)) {
        return {
          handled: true,
          reply: { text: "No active Finance edit session. Open a fresh review card and choose Edit." },
        };
      }
      return undefined;
    }
    const sessionStatus = lookup.session_status;
    if ((sessionStatus !== "active" && sessionStatus !== "expired" &&
         sessionStatus !== "completed_replay") ||
        lookup.active !== (sessionStatus === "active") ||
        typeof lookup.session_public_id !== "string" ||
        !/^gedit_[0-9a-f]{32}$/u.test(lookup.session_public_id) ||
        typeof lookup.proposal_public_id !== "string" ||
        !PROPOSAL_ID.test(lookup.proposal_public_id) ||
        typeof lookup.proposal_version !== "number" ||
        !Number.isSafeInteger(lookup.proposal_version) || lookup.proposal_version < 0 ||
        typeof lookup.effective_content_hash !== "string" ||
        !/^[0-9a-f]{64}$/u.test(lookup.effective_content_hash) ||
        typeof lookup.expires_at !== "number" || !Number.isSafeInteger(lookup.expires_at) ||
        lookup.expires_at <= 0 || typeof lookup.recovery_required !== "boolean" ||
        lookup.final_transaction_created !== false) {
      throw new Error("Guided edit session result is invalid.");
    }
    const sessionPublicId = lookup.session_public_id;
    const instructions =
      "Reply with one field=value message. Supported fields: 金额, 币种, 日期, 商户, 描述, 分类. " +
      "Use 日期=YYYY-MM-DD. Reply 完成 when finished.";
    if (hasMedia) {
      return { handled: true, reply: { text: `Receipt images are not accepted during editing. ${instructions}` } };
    }
    if (parsed.kind === "invalid") {
      return { handled: true, reply: { text: `Edit was not applied. ${instructions}` } };
    }
    if (parsed.kind === "complete") {
      if (sessionStatus === "expired") {
        return { handled: true, reply: { text: "Finance edit session expired. Choose Edit again." } };
      }
      const response = await this.runner.run(createBridgeRequest(
        "complete_guided_edit",
        { ...context, session_public_id: sessionPublicId, telegram_message_id: turn.messageId },
        guidedEditCompleteKey(sessionPublicId, turn.messageId),
      ), deadline());
      if (response.status !== "ok") {
        return { handled: true, reply: { text: "Finance edit could not be completed safely." } };
      }
      if (response.result.session_public_id !== sessionPublicId ||
          response.result.session_status !== "completed_replay" ||
          response.result.active !== false || response.result.recovery_required !== false ||
          response.result.proposal_public_id !== lookup.proposal_public_id ||
          response.result.proposal_version !== lookup.proposal_version ||
          response.result.effective_content_hash !== lookup.effective_content_hash ||
          typeof response.result.review_batch_id !== "string" ||
          !/^[0-9a-f]{32}$/u.test(response.result.review_batch_id) ||
          !isJsonObject(response.result.human_draft_card) ||
          response.result.final_transaction_created !== false) {
        throw new Error("Guided edit completion result is invalid.");
      }
      const card = requireActionableHumanDraftCard(response.result.human_draft_card);
      if (card.proposalPublicId !== response.result.proposal_public_id ||
          card.proposalVersion !== response.result.proposal_version ||
          card.proposalContentHash !== response.result.effective_content_hash) {
        throw new Error("Guided edit D1 card differs from the completed proposal.");
      }
      return await this.deliverHumanDraftPostingReview(card, turn, deadline);
    }
    const response = await this.runner.run(createBridgeRequest(
      "apply_guided_edit_update",
      {
        ...context,
        session_public_id: sessionPublicId,
        telegram_message_id: turn.messageId,
        field_name: parsed.field,
        field_value: parsed.value,
      },
      guidedEditUpdateKey(sessionPublicId, turn.messageId),
    ), deadline());
    if (response.status !== "ok") {
      return {
        handled: true,
        reply: { text: `Edit was not applied. Check the field and value. ${instructions}` },
      };
    }
    if (response.result.session_public_id !== sessionPublicId ||
        typeof response.result.proposal_public_id !== "string" ||
        !PROPOSAL_ID.test(response.result.proposal_public_id) ||
        typeof response.result.proposal_version !== "number" ||
        !Number.isSafeInteger(response.result.proposal_version) ||
        response.result.proposal_version < 0 ||
        typeof response.result.effective_content_hash !== "string" ||
        !/^[0-9a-f]{64}$/u.test(response.result.effective_content_hash) ||
        (response.result.edit_kind !== "completion" &&
         response.result.edit_kind !== "receipt_monetary_correction" &&
         response.result.edit_kind !== "guided_replay") ||
        typeof response.result.parse_status !== "string" ||
        response.result.final_transaction_created !== false) {
      throw new Error("Guided edit update result is invalid.");
    }
    return {
      handled: true,
      reply: { text: `${parsed.field} was updated. ${instructions}` },
    };
  }

  private async runReceiptTurn(
    turn: ValidatedTextTurn,
    metadata: Record<string, unknown>,
    admittedDeadlineMs: number,
  ): Promise<PluginHookInboundClaimResult> {
    if (this.receipt === undefined) throw new Error("Receipt intake is unavailable.");
    const startedAt = performance.now();
    const caption = receiptCaption(turn.text);
    const deadline = (): number => {
      const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
      if (remaining <= 0) throw new Error("Finance controller deadline exceeded.");
      return Math.min(COMMAND_DEADLINE_MS, remaining);
    };
    const key = canonicalCaptureKey(String(turn.chatId), String(turn.messageId));
    const identity = captureIdentities(key).rawIntakePublicId;
    const captureMedia = async (
      media: ValidatedMedia,
      published: { handoffFilename: string },
      payloadFd: number,
    ): Promise<JsonObject> => requireOk(await this.runner.run(createBridgeRequest(
      "capture",
      {
        workspace_path: this.workspaceRoot,
        kind: "receipt_image",
        handoff_filename: published.handoffFilename,
        handoff_descriptor_fd: 3,
        handoff_content_hash: media.contentHash,
        telegram_message_id: turn.messageId,
        telegram_chat_id: turn.chatId,
        telegram_message_date: turn.date,
        sender_id: turn.senderId,
        authenticated_actor_id: String(turn.senderId),
        telegram_account_id: turn.accountId,
        telegram_conversation_id: String(turn.chatId),
        conversation_binding_id: turn.bindingId,
        declared_mime_type: media.detectedMimeType,
        ...(media.originalFilename === undefined
          ? {}
          : { original_filename: media.originalFilename }),
        ...(caption === undefined ? {} : { caption }),
      },
      key,
    ), deadline(), payloadFd));
    let capture: JsonObject;
    try {
      const media = await this.receipt.media.acquire(metadata, deadline());
      capture = await this.receipt.handoff.withPublished(
        key,
        identity,
        media,
        async (published, payloadFd) => await captureMedia(media, published, payloadFd),
        deadline(),
      );
    } catch (error) {
      if (!(error instanceof ReceiptMediaUnavailableError)) throw error;
      const originalFilename = error.originalFilename;
      const declaredMimeType = error.declaredMimeType;
      const retainedCapture = await this.receipt.handoff.withRetained(
        key,
        identity,
        async (published, payloadFd, media) => {
          if (declaredMimeType !== undefined && declaredMimeType !== media.detectedMimeType) {
            throw new Error("Declared receipt MIME does not match retained evidence.");
          }
          if (originalFilename !== undefined) {
            const extension = extname(originalFilename).toLowerCase();
            if (extension !== media.canonicalExtension &&
                !(media.canonicalExtension === ".jpg" && extension === ".jpeg")) {
              throw new Error("Declared receipt extension does not match retained evidence.");
            }
          }
          return await captureMedia(
            originalFilename === undefined ? media : { ...media, originalFilename },
            published,
            payloadFd,
          );
        },
        deadline(),
      );
      if (retainedCapture === undefined) throw error;
      capture = retainedCapture;
    }
    return await this.proposeAndReviewCaptured(capture, turn, deadline);
  }

  private async runTextTurn(
    turn: ValidatedTextTurn,
    admittedDeadlineMs: number,
  ): Promise<PluginHookInboundClaimResult> {
    const startedAt = performance.now();
    const deadline = (): number => {
      const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
      if (remaining <= 0) throw new Error("Finance controller deadline exceeded.");
      return Math.min(COMMAND_DEADLINE_MS, remaining);
    };
    const key = canonicalCaptureKey(String(turn.chatId), String(turn.messageId));
    const capture = requireOk(await this.runner.run(createBridgeRequest(
      "capture",
      {
        workspace_path: this.workspaceRoot,
        kind: "text",
        telegram_message: {
          message_id: turn.messageId,
          chat: { id: turn.chatId, type: "private" },
          date: turn.date,
          from: { id: turn.senderId },
          text: turn.text,
        },
        authenticated_actor_id: String(turn.senderId),
        telegram_account_id: turn.accountId,
        telegram_conversation_id: String(turn.chatId),
        conversation_binding_id: turn.bindingId,
      },
      key,
    ), deadline()));
    return await this.proposeAndReviewCaptured(capture, turn, deadline);
  }

  private async proposeAndReviewCaptured(
    capture: JsonObject,
    turn: ValidatedTextTurn,
    deadline: () => number,
  ): Promise<PluginHookInboundClaimResult> {
    const intakePublicId = requirePublicId(capture, "intake_public_id", "intake");
    try {
      return await this.proposeAndReview(capture, turn, deadline);
    } catch {
      return {
        handled: true,
        reply: {
          text: `${FINANCE_FAILURE_REPLY}\n\nFinance intake: ${intakePublicId}\n${await this.processingFooter(intakePublicId, deadline)}`,
        },
      };
    }
  }

  private async proposeAndReview(
    capture: JsonObject,
    turn: ValidatedTextTurn,
    deadline: () => number,
  ): Promise<PluginHookInboundClaimResult> {
    const intakePublicId = requirePublicId(capture, "intake_public_id", "intake");
    const proposed = requireOk(await this.runner.run(createBridgeRequest(
      "propose",
      { workspace_path: this.workspaceRoot, intake_public_id: intakePublicId },
      `bridge-propose:${intakePublicId}`,
    ), deadline()));
    const deterministicProposalPublicId = requirePublicId(
      proposed,
      "proposal_public_id",
      "proposal",
    );
    let fallback: AiFallbackOutcome;
    try {
      fallback = await this.runAiFallback(intakePublicId, deadline);
    } catch {
      return {
        handled: true,
        reply: {
          text: `${FINANCE_FAILURE_REPLY}\n\nFinance intake: ${intakePublicId}\n${await this.processingFooter(intakePublicId, deadline)}`,
        },
      };
    }
    if (fallback.kind === "stopped") {
      const processingFooter = await this.processingFooter(intakePublicId, deadline);
      return {
        handled: true,
        reply: {
          text: `${fallback.reason === "classification_only"
            ? "Finance could not verify this message as a personal expense. Please retry with clearer expense wording."
            : FINANCE_FAILURE_REPLY}\n\nFinance intake: ${intakePublicId}\n${processingFooter}`,
        },
      };
    }
    const proposalPublicId = fallback.kind === "proposal"
      ? fallback.proposalPublicId
      : deterministicProposalPublicId;
    return await this.reviewProposal(proposalPublicId, turn, deadline, intakePublicId);
  }

  private async deliverHumanDraftPostingReview(
    card: ValidatedHumanDraftCard,
    turn: ValidatedTextTurn,
    deadline: () => number,
  ): Promise<PluginHookInboundClaimResult> {
    if (!card.confirmAvailable || card.completeness !== "complete" ||
        card.cardReference !== card.currentCardReference) {
      throw new Error("D2 delivery requires the complete current D1 card.");
    }
    if (turn.sessionKey !== turn.bindingId) {
      throw new Error("D2 terminal delivery session does not match the private binding.");
    }
    const commandContext = {
      workspace_path: this.workspaceRoot,
      operator_actor_id: String(turn.senderId),
      telegram_account_id: turn.accountId,
      telegram_conversation_id: String(turn.chatId),
      conversation_binding_id: turn.bindingId,
    };
    const prepared = requireOk(await this.runner.run(createBridgeRequest(
      "prepare_posting_review",
      {
        ...commandContext,
        card_generation_public_id: card.cardReference,
      },
      postingReviewPreparationKey(card.cardReference),
    ), deadline()));
    const review = requirePostingReview(prepared, card);
    const postingActions = requireOk(await this.runner.run(createBridgeRequest(
      "issue_posting_review_actions",
      {
        ...commandContext,
        posting_review_public_id: review.reviewPublicId,
      },
      postingActionIssuanceKey(review.reviewPublicId),
    ), deadline()));
    const delivery = requirePostingDeliveryManifest(postingActions, review);
    return {
      handled: true,
      reply: {
        text: delivery.text,
        channelData: {
          telegram: {
            buttons: delivery.buttons,
            financeDeliveryMaterialV1: { attemptNonce: delivery.attemptNonce },
          },
        },
      },
    };
  }

  private async reviewProposal(
    proposalPublicId: string,
    turn: ValidatedTextTurn,
    deadline: () => number,
    intakePublicId?: string,
    guidedRecovery?: { sessionPublicId: string; messageId: number; batchId: string },
  ): Promise<PluginHookInboundClaimResult> {
    const review = requireOk(await this.runner.run(createBridgeRequest(
      "get_review",
      { workspace_path: this.workspaceRoot, proposal_public_id: proposalPublicId },
    ), deadline()));
    if (requirePublicId(review, "proposal_public_id", "proposal") !== proposalPublicId) {
      throw new Error("Review proposal identity does not match propose result.");
    }
    if (typeof review.confirm_available !== "boolean") {
      throw new Error("Review confirmation availability is invalid.");
    }
    const confirmAvailable = review.confirm_available;
    const reviewBinding = requireReviewBinding(review);
    const renderedReview = renderReview(review, confirmAvailable);
    if (confirmAvailable) {
      if (turn.sessionKey !== turn.bindingId) {
        throw new Error("D2 terminal delivery session does not match the private binding.");
      }
      const commandContext = {
        workspace_path: this.workspaceRoot,
        operator_actor_id: String(turn.senderId),
        telegram_account_id: turn.accountId,
        telegram_conversation_id: String(turn.chatId),
        conversation_binding_id: turn.bindingId,
      };
      const prepared = requireOk(await this.runner.run(createBridgeRequest(
        "prepare_posting_review",
        {
          ...commandContext,
          proposal_public_id: proposalPublicId,
          admitted_source_message_id: String(turn.messageId),
        },
        initialPostingReviewPreparationKey(proposalPublicId, turn.messageId),
      ), deadline()));
      const postingReview = requireInitialPostingReview(
        prepared,
        proposalPublicId,
        review,
        reviewBinding,
      );
      const postingManifest = requireOk(await this.runner.run(createBridgeRequest(
        "issue_posting_review_actions",
        {
          ...commandContext,
          posting_review_public_id: postingReview.reviewPublicId,
        },
        postingActionIssuanceKey(postingReview.reviewPublicId),
      ), deadline()));
      const delivery = requirePostingDeliveryManifest(postingManifest, postingReview);
      return {
        handled: true,
        reply: {
          text: delivery.text,
          channelData: {
            telegram: {
              buttons: delivery.buttons,
              financeDeliveryMaterialV1: { attemptNonce: delivery.attemptNonce },
            },
          },
        },
      };
    }
    const text = intakePublicId === undefined
      ? renderedReview
      : `${renderedReview}\n\nFinance intake: ${intakePublicId}\n${await this.processingFooter(intakePublicId, deadline)}`;
    let batchId = guidedRecovery?.batchId ?? createHumanActionBatchId();
    const issueBatch = async (currentBatchId: string): Promise<BridgeResponse> => {
      return await this.runner.run(createBridgeRequest(
        "issue_human_actions",
        {
          workspace_path: this.workspaceRoot,
          proposal_public_id: proposalPublicId,
          operator_actor_id: String(turn.senderId),
          telegram_account_id: turn.accountId,
          telegram_conversation_id: String(turn.chatId),
          conversation_binding_id: turn.bindingId,
          reference_batch_id: currentBatchId,
          token_ttl_seconds: 600,
          expected_proposal_version: reviewBinding.version,
          expected_content_hash: reviewBinding.contentHash,
          ...(guidedRecovery === undefined ? {} : {
            minimum_remaining_seconds: 60,
            require_unconsumed_replay: true,
          }),
        },
        humanActionIssuanceKey(currentBatchId),
      ), deadline());
    };
    let issuedResponse = await issueBatch(batchId);
    if (guidedRecovery !== undefined && issuedResponse.status === "error" &&
        issuedResponse.error.code === "CALLBACK_EXPIRED") {
      const renewed = await this.runner.run(createBridgeRequest(
        "complete_guided_edit",
        {
          workspace_path: this.workspaceRoot,
          operator_actor_id: String(turn.senderId),
          telegram_account_id: turn.accountId,
          telegram_conversation_id: String(turn.chatId),
          conversation_binding_id: turn.bindingId,
          session_public_id: guidedRecovery.sessionPublicId,
          telegram_message_id: guidedRecovery.messageId,
        },
        guidedEditCompleteKey(guidedRecovery.sessionPublicId, guidedRecovery.messageId),
      ), deadline());
      if (renewed.status !== "ok" ||
          renewed.result.session_public_id !== guidedRecovery.sessionPublicId ||
          renewed.result.session_status !== "completed_replay" ||
          renewed.result.active !== false || renewed.result.recovery_required !== false ||
          renewed.result.proposal_public_id !== proposalPublicId ||
          renewed.result.proposal_version !== reviewBinding.version ||
          renewed.result.effective_content_hash !== reviewBinding.contentHash ||
          typeof renewed.result.review_batch_id !== "string" ||
          !/^[0-9a-f]{32}$/u.test(renewed.result.review_batch_id) ||
          renewed.result.review_batch_id === batchId ||
          renewed.result.final_transaction_created !== false) {
        throw new Error("Guided edit review renewal result is invalid.");
      }
      batchId = renewed.result.review_batch_id;
      issuedResponse = await issueBatch(batchId);
    }
    const issued = requireOk(issuedResponse);
    if (requirePublicId(issued, "proposal_public_id", "proposal") !== proposalPublicId ||
        issued.proposal_version !== reviewBinding.version ||
        issued.content_hash !== reviewBinding.contentHash ||
        issued.final_transaction_created !== false) {
      throw new Error("Human action issuance identity mismatch.");
    }
    const references = requireActionReferences(issued, false);
    if (references.reject === undefined) {
      throw new Error("Reject action reference is missing.");
    }
    const buttons = [];
    buttons.push({
      label: "Reject",
      style: "danger" as const,
      action: { type: "callback" as const, value: humanActionCallbackData("reject", references.reject) },
    });
    return {
      handled: true,
      reply: {
        presentation: {
          blocks: [
            { type: "text", text },
            {
              type: "buttons",
              buttons,
            },
          ],
        },
      },
    };
  }

  private async processingFooter(
    intakePublicId: string,
    deadline: () => number,
  ): Promise<string> {
    try {
      const response = await this.runner.run(createBridgeRequest(
        "get_ai_processing_status_v2",
        { workspace_path: this.workspaceRoot, intake_public_id: intakePublicId },
      ), deadline());
      return renderProcessingFooterV2(parseProcessingStatusV2(requireOk(response)));
    } catch {
      return "🛑 状态暂不可用";
    }
  }

  private async runAiFallback(
    intakePublicId: string,
    deadline: () => number,
  ): Promise<AiFallbackOutcome> {
    if (this.llmRuntime === undefined) return { kind: "unavailable" };
    const recordStage = (
      stage: "config_projection" | "receipt_lookup_and_bridge_prepare" |
        "host_completion" | "result_persistence",
      startedAt: number,
    ): void => {
      const elapsedMs = Math.max(0, Math.min(120_000, Math.round(performance.now() - startedAt)));
      try {
        this.llmRuntime?.recordStageTiming?.(stage, elapsedMs);
      } catch {
        // Public-safe telemetry must never change Finance processing behavior.
      }
    };
    const projectionStartedAt = performance.now();
    let configProjection: FinanceAgentConfigProjectionV2 | FinanceAgentConfigRefusalV2;
    try {
      configProjection = this.llmRuntime.currentProjection();
    } finally {
      recordStage("config_projection", projectionStartedAt);
    }
    const prepareStartedAt = performance.now();
    let preparedResponse: BridgeResponse;
    try {
      preparedResponse = await this.runner.run(createBridgeRequest(
        "prepare_ai_fallback_v2",
        {
          workspace_path: this.workspaceRoot,
          intake_public_id: intakePublicId,
          config_projection: configProjection as unknown as JsonValue,
        },
        aiFallbackKey("prepare_ai_fallback_v2", intakePublicId),
      ), deadline());
    } finally {
      recordStage("receipt_lookup_and_bridge_prepare", prepareStartedAt);
    }
    if (preparedResponse.status !== "ok") {
      if (preparedResponse.error.code === "AI_FALLBACK_NOT_ELIGIBLE") {
        return { kind: "unavailable" };
      }
      if ([
        "AI_MODEL_CONFIG_REFUSED",
        "AI_MODEL_CONFIG_NOT_ACCEPTED",
        "AI_MODEL_COMPATIBILITY_POLICY_REFUSED",
        "AI_MODEL_COMPATIBILITY_CONFLICT",
      ].includes(preparedResponse.error.code)) {
        return { kind: "stopped", reason: "manual_recovery" };
      }
      throw new Error(`AI fallback preparation refused with ${preparedResponse.error.code}.`);
    }
    const prepared = preparedResponse.result;
    const attemptPublicId = requireAiPublicId(prepared, "attempt_public_id", /^aifa_[0-9a-f]{64}$/u);
    requireAiPublicId(prepared, "receipt_public_id", /^aimr_[0-9a-f]{64}$/u);
    if (typeof prepared.config_projection_hash !== "string" ||
        !/^[0-9a-f]{64}$/u.test(prepared.config_projection_hash)) {
      throw new Error("AI fallback receipt projection hash is invalid.");
    }
    if (prepared.claim_disposition !== "claim_once") {
      const replayView = prepared.replay_view;
      if (isJsonObject(replayView) && replayView.view_kind === "result" &&
          isJsonObject(replayView.result) &&
          typeof replayView.result.proposal_public_id === "string") {
        return { kind: "proposal", proposalPublicId: replayView.result.proposal_public_id };
      }
      if (isJsonObject(replayView) && replayView.view_kind === "result" &&
          isJsonObject(replayView.result) &&
          replayView.result.result_status === "classification_only") {
        return { kind: "stopped", reason: "classification_only" };
      }
      return { kind: "stopped", reason: "manual_recovery" };
    }

    const claimedResponse = await this.runner.run(createBridgeRequest(
      "claim_ai_fallback_invocation_v2",
      { workspace_path: this.workspaceRoot, attempt_public_id: attemptPublicId },
      aiFallbackKey("claim_ai_fallback_invocation_v2", attemptPublicId),
    ), deadline());
    if (claimedResponse.status !== "ok") {
      throw new Error(`AI fallback claim refused with ${claimedResponse.error.code}.`);
    }
    if (claimedResponse.result.invocation_disposition !== "invoke_once") {
      return { kind: "stopped", reason: "manual_recovery" };
    }
    const modelCall = requireFallbackModelCall(
      claimedResponse.result.model_call,
      prepared.request_identity,
    );
    const callStartNotAfter = claimedResponse.result.call_start_not_after_ms;
    if (typeof callStartNotAfter !== "number" || !Number.isSafeInteger(callStartNotAfter)) {
      requireOk(await this.runner.run(createBridgeRequest(
        "record_ai_fallback_result_v2",
        {
          workspace_path: this.workspaceRoot,
          attempt_public_id: attemptPublicId,
          transport_outcome: "local_preinvocation_refused",
          failure_code: "request_integrity_refused",
        },
        aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId),
      ), deadline()));
      return { kind: "stopped", reason: "manual_recovery" };
    }
    const resultNotAfterMs = prepared.result_not_after_ms;
    if (typeof resultNotAfterMs !== "number" || !Number.isSafeInteger(resultNotAfterMs)) {
      requireOk(await this.runner.run(createBridgeRequest(
        "record_ai_fallback_result_v2",
        {
          workspace_path: this.workspaceRoot,
          attempt_public_id: attemptPublicId,
          transport_outcome: "local_preinvocation_refused",
          failure_code: "request_integrity_refused",
        },
        aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId),
      ), deadline()));
      return { kind: "stopped", reason: "manual_recovery" };
    }
    const terminal = new AbortController();
    let closed = false;
    let terminalSubmission: Promise<JsonObject> | undefined;
    let terminalReason: "timeout" | "settled" | undefined;
    let timeout: NodeJS.Timeout | undefined;
    const submit = async (
      transportOutcome: string,
      details: JsonObject = {},
    ): Promise<JsonObject> => {
      if (terminalSubmission !== undefined) return terminalSubmission;
      terminalReason = transportOutcome === "timeout"
        ? "timeout"
        : "settled";
      closed = true;
      terminal.abort(terminalReason);
      terminalSubmission = (async () => {
        const resultStartedAt = performance.now();
        try {
          return requireOk(await this.runner.run(createBridgeRequest(
            "record_ai_fallback_result_v2",
            {
              workspace_path: this.workspaceRoot,
              attempt_public_id: attemptPublicId,
              transport_outcome: transportOutcome,
              ...details,
            },
            aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId),
          ), deadline()));
        } finally {
          recordStage("result_persistence", resultStartedAt);
        }
      })();
      return terminalSubmission;
    };
    if (Date.now() >= callStartNotAfter) {
      await submit("local_preinvocation_refused", {
        failure_code: "call_start_deadline_exceeded",
      });
      terminal.abort("late");
      return { kind: "stopped", reason: "manual_recovery" };
    }
    const callStartedAtMs = Date.now();
    const outerRemainingMs = deadline();
    const resultGuardRemainingMs = resultNotAfterMs - callStartedAtMs - 5_000;
    const invocationWindow = Math.min(
      outerRemainingMs,
      30_000,
      resultGuardRemainingMs,
    );
    if (invocationWindow <= 0) {
      await submit("timeout", { failure_code: "deadline_exceeded" });
      return { kind: "stopped", reason: "manual_recovery" };
    }
    const timeoutPromise = new Promise<never>((_resolve, reject) => {
      timeout = setTimeout(() => {
        void submit("timeout", { failure_code: "deadline_exceeded" }).catch(() => undefined);
        reject(new Error("AI fallback timed out."));
      }, invocationWindow);
    });
    try {
      const completionStartedAt = performance.now();
      let completion: FinanceLlmCompletion;
      try {
        completion = await Promise.race([
          this.llmRuntime.complete({
            ...modelCall,
            maxRetries: 0,
            signal: terminal.signal,
          }),
          timeoutPromise,
        ]);
      } finally {
        recordStage("host_completion", completionStartedAt);
      }
      if (typeof completion.text !== "string") {
        const recorded = await submit("response_metadata_refused", {
          metadata_field: "provider",
          metadata_reason: "invalid_type",
          metadata_code_unit_count: null,
          metadata_sha256: null,
          response_body_state: "none",
        });
        return aiFallbackOutcome(recorded);
      }
      const text = completion.text;
      const codeUnits = text.length;
      const issue = metadataIssue(completion);
      const sessionKey = completion.audit?.sessionKey;
      let transportOutcome: string;
      let details: JsonObject = {
        returned_provider: completion.provider ?? null,
        returned_model: completion.model ?? null,
        returned_agent_id: completion.agentId ?? null,
        audit_caller_kind: completion.audit?.caller?.kind ?? null,
        audit_caller_id: completion.audit?.caller?.id ?? null,
        audit_caller_name: completion.audit?.caller?.name ?? null,
        audit_purpose: completion.audit?.purpose ?? null,
        audit_session_key_sha256: sessionKeyHash(
          typeof sessionKey === "string" ? sessionKey : undefined,
        ),
        usage_input_tokens: completion.usage?.inputTokens ?? null,
        usage_output_tokens: completion.usage?.outputTokens ?? null,
      };
      if (issue !== undefined) {
        const bodyState = codeUnits > 131_072
          ? "resource_refused"
          : hasUnpairedSurrogate(text)
            ? "unencodable"
            : Buffer.byteLength(text, "utf8") > 65_536
              ? "oversize"
              : "retained";
        const body: JsonObject = { response_body_state: bodyState };
        if (bodyState === "resource_refused") {
          body.response_code_unit_count = codeUnits;
        } else if (bodyState === "unencodable") {
          body.response_code_unit_count = codeUnits;
          body.response_utf16_sha256 = utf16Sha256(text);
        } else {
          const encoded = Buffer.from(text, "utf8");
          body.response_utf8_b64 = encoded.toString("base64");
          body.response_byte_count = encoded.byteLength;
          body.response_sha256 = createHash("sha256").update(encoded).digest("hex");
          if (bodyState === "oversize") {
            delete body.response_utf8_b64;
            body.response_code_unit_count = codeUnits;
          }
        }
        transportOutcome = "response_metadata_refused";
        details = {
          metadata_field: issue.field,
          metadata_reason: issue.reason,
          metadata_code_unit_count: issue.codeUnitCount,
          metadata_sha256: issue.sha256,
          ...body,
        };
      } else if (codeUnits > 131_072) {
        transportOutcome = "response_resource_refused";
        details.response_code_unit_count = codeUnits;
      } else if (hasUnpairedSurrogate(text)) {
        transportOutcome = "response_unencodable";
        details.response_code_unit_count = codeUnits;
        details.response_utf16_sha256 = utf16Sha256(text);
      } else {
        const encoded = Buffer.from(text, "utf8");
        if (encoded.byteLength > 65_536) {
          transportOutcome = "response_oversize";
          details.response_code_unit_count = codeUnits;
          details.response_byte_count = encoded.byteLength;
          details.response_sha256 = createHash("sha256").update(encoded).digest("hex");
        } else {
          transportOutcome = "response_received";
          details.response_utf8_b64 = encoded.toString("base64");
          details.response_byte_count = encoded.byteLength;
          details.response_sha256 = createHash("sha256").update(encoded).digest("hex");
        }
      }
      terminalReason = "settled";
      terminal.abort("settled");
      const recorded = await submit(transportOutcome, details);
      return aiFallbackOutcome(recorded);
    } catch (error) {
      if (terminalSubmission !== undefined) {
        await terminalSubmission.catch(() => undefined);
      } else if (!closed) {
        await submit("provider_error", { failure_code: "host_llm_failed" }).catch(() => undefined);
      }
      return { kind: "stopped", reason: "manual_recovery" };
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
      if (!terminal.signal.aborted) terminal.abort("settled");
    }
  }
}

function aiFallbackKey(command: string, publicId: string): string {
  const domain = command === "prepare_ai_fallback_v2"
    ? "finance-aifp-key-v2"
    : command === "claim_ai_fallback_invocation_v2"
      ? "finance-aifc-key-v2"
      : "finance-aifr-key-v2";
  const prefix = command === "prepare_ai_fallback_v2"
    ? "aifp2_"
    : command === "claim_ai_fallback_invocation_v2"
      ? "aifc2_"
      : "aifr2_";
  const digest = createHash("sha256")
    .update(domain, "ascii")
    .update("\0", "ascii")
    .update(Buffer.from([0, 0, 0, 2]))
    .update(Buffer.from([0, 0, 0, 0, 0, 0, 0, command.length]))
    .update(command, "utf8")
    .update(Buffer.from([0, 0, 0, 0, 0, 0, 0, publicId.length]))
    .update(publicId, "utf8")
    .digest("hex");
  return `${prefix}${digest}`;
}

function requireAiPublicId(
  object: JsonObject,
  field: string,
  pattern: RegExp,
): string {
  const value = object[field];
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`AI fallback result ${field} is invalid.`);
  }
  return value;
}

function isReceiptMetadata(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const metadata = value as Record<string, unknown>;
  return [
    "mediaStagingPending",
    "mediaUrl",
    "mediaUrls",
    "mediaPath",
    "mediaPaths",
    "mediaType",
    "mediaTypes",
    "originalFilename",
  ].some((field) => metadata[field] !== undefined);
}
