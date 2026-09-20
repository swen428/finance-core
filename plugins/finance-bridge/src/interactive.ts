import type { PluginConversationBinding } from "openclaw-sdk/plugin-sdk/plugin-entry";

import type { BridgeRunner } from "./controller.js";
import {
  createBridgeRequest,
  framedDigest,
  humanDraftDeliveryAttemptId,
  humanDraftDeliveryKey,
  humanDraftObservationId,
  humanDraftObservationKey,
  humanActionIssuanceKey,
  humanActionRedemptionKey,
  postingConfirmationKey,
  postingActionIssuanceKey,
  postingReviewPreparationKey,
  postingResumeKey,
  type BridgeResponse,
  type JsonObject,
} from "./protocol.js";
import { renderWholeCard, type WholeCardFields } from "./whole-card.js";

export const DISABLED_REPLY = "Current action is not enabled.";
export const DISABLED_ACTIONS = [
  "edit-disabled",
] as const;
export const ACTIVE_ACTIONS = ["post", "confirm", "edit", "reject"] as const;
export const ACTION_FAILURE_REPLY =
  "Finance action could not be applied safely. Request a fresh review.";
export const ACTION_OUTCOME_UNKNOWN_REPLY =
  "Finance decision outcome could not be verified. Finalization did not run; do not retry " +
  "from this card until the durable proposal status is checked.";
export const POSTING_OUTCOME_UNKNOWN_REPLY =
  "Finance posting outcome could not be verified. It may already have completed; do not " +
  "press Confirm again until the durable posting status is available.";
export const POSTING_NEEDS_ATTENTION_REPLY =
  "Finance posting needs attention. No new confirmation was created; request the current " +
  "Finance record.";
export const EDIT_PRESENTATION_FAILURE_REPLY =
  "Finance edit session was started, but the updated card could not be displayed safely. " +
  "Request the current Finance record.";

export type DisabledAction = (typeof DISABLED_ACTIONS)[number];
export type ActiveAction = (typeof ACTIVE_ACTIONS)[number];

interface DisabledContext {
  accountId: string;
  conversationId: string;
  senderId: string;
  payload: DisabledAction;
  reply(text: string): Promise<void>;
  currentBinding(): Promise<PluginConversationBinding | null>;
}

interface BindingContext {
  accountId: string;
  conversationId: string;
  senderId: string;
}

interface ActiveContext {
  accountId: string;
  callbackId: string;
  callbackMessageId: number;
  conversationId: string;
  senderId: string;
  sessionKey?: string;
  action: ActiveAction;
  reference: string;
  reply(text: string): Promise<void>;
  replace(
    text: string,
    buttons: TelegramInteractiveButtons,
    financeDeliveryAttemptNonce?: string,
  ): Promise<void>;
  currentBinding(): Promise<PluginConversationBinding | null>;
}

type TelegramInteractiveButtons = Array<Array<{
  text: string;
  callback_data: string;
  style?: "danger" | "success" | "primary";
}>>;

export interface HumanActionRuntime {
  workspaceRoot: string;
  runner: BridgeRunner;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isDisabledAction(value: unknown): value is DisabledAction {
  return typeof value === "string" && (DISABLED_ACTIONS as readonly string[]).includes(value);
}

function isCanonicalTelegramPrivateId(value: unknown): value is string {
  return typeof value === "string" && value.length <= 32 && /^[1-9][0-9]*$/u.test(value);
}

function isCanonicalHostIdentifier(value: unknown): value is string {
  return typeof value === "string" && /^[\x21-\x7e]{1,200}$/u.test(value);
}

function parseActivePayload(value: unknown): {action: ActiveAction; reference: string} | undefined {
  if (typeof value !== "string") return undefined;
  const match = /^(post|confirm|edit|reject):(fha1_[A-Za-z0-9_-]{24})$/u.exec(value);
  if (match === null) return undefined;
  return { action: match[1] as ActiveAction, reference: match[2]! };
}

function narrowContext(value: unknown): DisabledContext | undefined {
  if (!isRecord(value) || value.channel !== "telegram" || value.isGroup !== false ||
      value.isForum !== false || value.threadId !== undefined ||
      (value.parentConversationId !== undefined &&
        value.parentConversationId !== value.conversationId) || !isRecord(value.auth) ||
      value.auth.isAuthorizedSender !== true || typeof value.accountId !== "string" ||
      typeof value.conversationId !== "string" || typeof value.senderId !== "string" ||
      value.senderId !== value.conversationId || !isRecord(value.callback) ||
      value.callback.namespace !== "finance-bridge" ||
      !isDisabledAction(value.callback.payload) ||
      value.callback.data !== disabledCallbackData(value.callback.payload) ||
      value.callback.chatId !== value.conversationId || !isRecord(value.respond) ||
      typeof value.respond.reply !== "function" ||
      typeof value.getCurrentConversationBinding !== "function") {
    return undefined;
  }
  const respond = value.respond;
  return {
    accountId: value.accountId,
    conversationId: value.conversationId,
    senderId: value.senderId,
    ...(typeof value.sessionKey === "string" ? { sessionKey: value.sessionKey } : {}),
    payload: value.callback.payload,
    reply: async (text) => await (respond.reply as (params: {text: string}) => Promise<void>)({ text }),
    currentBinding: value.getCurrentConversationBinding as () => Promise<PluginConversationBinding | null>,
  };
}

function bindingMatches(context: BindingContext, binding: PluginConversationBinding): boolean {
  if (!isCanonicalTelegramPrivateId(context.senderId) ||
      context.conversationId !== context.senderId ||
      !isCanonicalHostIdentifier(context.accountId) ||
      !isCanonicalHostIdentifier(binding.bindingId) ||
      binding.pluginId !== "finance-bridge" || binding.channel !== "telegram" ||
      binding.accountId !== context.accountId || binding.conversationId !== context.conversationId ||
      (binding.parentConversationId !== undefined &&
        binding.parentConversationId !== binding.conversationId) || binding.threadId !== undefined ||
      !isRecord(binding.data)) return false;
  return binding.data.senderId === context.senderId;
}

function activeBindingMatches(context: ActiveContext, binding: PluginConversationBinding): boolean {
  return bindingMatches(context, binding);
}

export function disabledCallbackData(action: DisabledAction): string {
  const data = `finance-bridge:${action}`;
  if (Buffer.byteLength(data, "utf8") > 64) throw new Error("Callback data exceeds 64 bytes.");
  return data;
}

export function humanActionCallbackData(action: ActiveAction, reference: string): string {
  if (!/^fha1_[A-Za-z0-9_-]{24}$/u.test(reference)) {
    throw new Error("Human action reference is invalid.");
  }
  const data = `finance-bridge:${action}:${reference}`;
  if (Buffer.byteLength(data, "utf8") > 64) throw new Error("Callback data exceeds 64 bytes.");
  return data;
}

export function postingActionCallbackData(reference: string): string {
  return humanActionCallbackData("post", reference);
}

function narrowActiveContext(value: unknown): ActiveContext | undefined {
  if (!isRecord(value) || value.channel !== "telegram" || value.isGroup !== false ||
      value.isForum !== false || value.threadId !== undefined ||
      (value.parentConversationId !== undefined &&
        value.parentConversationId !== value.conversationId) || !isRecord(value.auth) ||
      value.auth.isAuthorizedSender !== true || !isCanonicalHostIdentifier(value.accountId) ||
      typeof value.callbackId !== "string" || value.callbackId.length === 0 ||
      value.callbackId.length > 200 || !isCanonicalTelegramPrivateId(value.conversationId) ||
      !isCanonicalTelegramPrivateId(value.senderId) || value.senderId !== value.conversationId ||
      !isRecord(value.callback) ||
      typeof value.callback.messageId !== "number" || !Number.isSafeInteger(value.callback.messageId) ||
      value.callback.messageId <= 0 || value.callback.chatId !== value.conversationId ||
      !isRecord(value.respond) || typeof value.respond.reply !== "function" ||
      typeof value.respond.editMessage !== "function" ||
      typeof value.getCurrentConversationBinding !== "function") return undefined;
  let parsed: {action: ActiveAction; reference: string} | undefined;
  let expectedData: string | undefined;
  if (value.callback.namespace === "finance-bridge") {
    parsed = parseActivePayload(value.callback.payload);
    if (parsed !== undefined) expectedData = humanActionCallbackData(parsed.action, parsed.reference);
  } else if ((ACTIVE_ACTIONS as readonly string[]).includes(String(value.callback.namespace)) &&
      typeof value.callback.payload === "string" &&
      /^fha1_[A-Za-z0-9_-]{24}$/u.test(value.callback.payload)) {
    const routedAction = value.callback.namespace === "post"
      ? "post"
      : value.callback.namespace as ActiveAction;
    parsed = { action: routedAction, reference: value.callback.payload };
    expectedData = `${value.callback.namespace}:${value.callback.payload}`;
  }
  if (parsed === undefined || value.callback.data !== expectedData) {
    return undefined;
  }
  const respond = value.respond;
  return {
    accountId: value.accountId,
    callbackId: value.callbackId,
    callbackMessageId: value.callback.messageId,
    conversationId: value.conversationId,
    senderId: value.senderId,
    ...(typeof value.sessionKey === "string" ? { sessionKey: value.sessionKey } : {}),
    action: parsed.action,
    reference: parsed.reference,
    reply: async (text) => await (respond.reply as (params: {text: string}) => Promise<void>)({ text }),
    replace: async (text, buttons, financeDeliveryAttemptNonce) => await (
      respond.editMessage as (
        params: {
          text: string;
          buttons: TelegramInteractiveButtons;
          financeDeliveryMaterialV1?: {attemptNonce: string};
        },
      ) => Promise<void>
    )({
      text,
      buttons,
      ...(financeDeliveryAttemptNonce === undefined
        ? {}
        : { financeDeliveryMaterialV1: { attemptNonce: financeDeliveryAttemptNonce } }),
    }),
    currentBinding: value.getCurrentConversationBinding as () => Promise<PluginConversationBinding | null>,
  };
}

function requireString(result: JsonObject, field: string): string {
  const value = result[field];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Human action ${field} is invalid.`);
  }
  return value;
}

function requireInteger(result: JsonObject, field: string): number {
  const value = result[field];
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`Human action ${field} is invalid.`);
  }
  return value;
}

interface RedeemedDraftCard {
  actionIssueBatchId: string;
  cardReference: string;
  confirmAvailable: boolean;
  fields: WholeCardFields;
  proposalContentHash: string;
  proposalPublicId: string;
  proposalVersion: number;
  text: string;
}

function renderRedeemedDraftCard(value: JsonObject): RedeemedDraftCard {
  const card = value.human_draft_card;
  if (!isRecord(card) || typeof card.card_generation_public_id !== "string" ||
      !/^d1card_[0-9a-f]{32}$/u.test(card.card_generation_public_id) ||
      card.current_card_generation_public_id !== card.card_generation_public_id ||
      (card.completeness !== "complete" && card.completeness !== "incomplete") ||
      typeof card.confirm_available !== "boolean" || card.reject_available !== true ||
      card.confirm_available !== (card.completeness === "complete") ||
      typeof card.action_issue_batch_id !== "string" ||
      !/^[0-9a-f]{64}$/u.test(card.action_issue_batch_id) ||
      card.refusal_code !== null ||
      !["started", "accepted", "noop"].includes(String(card.operation_outcome)) ||
      !["not_issued", "issued"].includes(String(card.action_issuance_state)) ||
      card.final_transaction_created !== false || !isRecord(card.field_values) ||
      Object.keys(card.field_values).sort().join(",") !==
        "amount,category,currency,description,merchant,transaction_date" ||
      !Object.values(card.field_values).every((field) => typeof field === "string") ||
      !Array.isArray(card.unresolved_flags) || card.unresolved_flags.length > 32 ||
      !card.unresolved_flags.every((flag) => typeof flag === "string" &&
        /^[a-z0-9_]{1,100}$/u.test(flag))) {
    throw new Error("Redeemed D1 card is invalid.");
  }
  const proposalPublicId = card.completeness === "complete"
    ? card.proposal_public_id
    : card.decision_target_proposal_public_id;
  const proposalVersion = card.completeness === "complete"
    ? card.proposal_version
    : card.decision_target_proposal_version;
  const proposalContentHash = card.completeness === "complete"
    ? card.proposal_content_hash
    : card.decision_target_proposal_content_hash;
  if (typeof proposalPublicId !== "string" ||
      !/^(?:po_d1_[0-9a-f]{32}|prop_bridge_[0-9a-f]{32}|parser_output_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u
        .test(proposalPublicId) ||
      typeof proposalVersion !== "number" || !Number.isSafeInteger(proposalVersion) ||
      proposalVersion < 0 || typeof proposalContentHash !== "string" ||
      !/^[0-9a-f]{64}$/u.test(proposalContentHash) ||
      (card.completeness === "incomplete" &&
       (card.proposal_public_id !== null || card.proposal_version !== null ||
        card.proposal_content_hash !== null))) {
    throw new Error("Redeemed D1 action authority is invalid.");
  }
  return {
    actionIssueBatchId: card.action_issue_batch_id,
    cardReference: card.card_generation_public_id,
    confirmAvailable: card.confirm_available,
    fields: card.field_values as WholeCardFields,
    proposalContentHash,
    proposalPublicId,
    proposalVersion,
    text: renderWholeCard({
    cardReference: card.card_generation_public_id,
    fields: card.field_values as WholeCardFields,
    language: "en",
    status: card.completeness === "complete" ? "publishable" : "incomplete",
    unresolvedReasons: card.unresolved_flags as string[],
    }),
  };
}

function requireRedeemedPostingReview(
  result: JsonObject,
  card: RedeemedDraftCard,
): {reviewPublicId: string; text: string} {
  const projection = result.visible_projection;
  if (typeof result.review_public_id !== "string" ||
      !/^d2rev_[0-9a-f]{30}$/u.test(result.review_public_id) ||
      result.card_generation_public_id !== card.cardReference ||
      result.proposal_public_id !== card.proposalPublicId ||
      result.proposal_version !== card.proposalVersion ||
      result.proposal_content_hash !== card.proposalContentHash ||
      (result.posting_path !== "text" && result.posting_path !== "personal_receipt") ||
      !isRecord(projection) || result.final_transaction_created !== false ||
      projection.amount !== card.fields.amount ||
      projection.currency !== card.fields.currency ||
      projection.transaction_date !== card.fields.transaction_date ||
      projection.merchant !== card.fields.merchant || projection.account !== "unspecified") {
    throw new Error("D2 edited posting review is invalid.");
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
  if (result.posting_path === "personal_receipt") {
    const calculation = projection.calculation;
    if (projection.receipt_total !== card.fields.amount ||
        projection.personal_share !== card.fields.amount || !isRecord(calculation) ||
        calculation.total_paid !== card.fields.amount ||
        typeof calculation.total_to_collect !== "string" ||
        !Array.isArray(calculation.settlement_obligations) ||
        calculation.settlement_obligations.length !== 0) {
      throw new Error("D2 edited receipt projection is invalid.");
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
  return { reviewPublicId: result.review_public_id, text: lines.join("\n") };
}

function requireRedeemedD2Buttons(
  posting: JsonObject,
  card: RedeemedDraftCard,
  reviewPublicId: string,
): {attemptNonce: string; buttons: TelegramInteractiveButtons; text: string} {
  const controls = posting.controls;
  if (posting.posting_review_public_id !== reviewPublicId ||
      typeof posting.delivery_attempt_public_id !== "string" ||
      !/^d2send_[0-9a-f]{32}$/u.test(posting.delivery_attempt_public_id) ||
      posting.delivery_manifest_version !== "finance_d2_controls_v1" ||
      posting.text !== card.text || typeof posting.delivery_attempt_nonce !== "string" ||
      !/^d2nonce_[0-9a-f]{32}$/u.test(posting.delivery_attempt_nonce) ||
      typeof posting.finance_delivery_material_sha256 !== "string" ||
      !/^[0-9a-f]{64}$/u.test(posting.finance_delivery_material_sha256) ||
      posting.final_transaction_created !== false || !Array.isArray(controls) ||
      controls.length !== 3) {
    throw new Error("D2 edited delivery manifest is invalid.");
  }
  const expected = [
    { action: "confirm", label: "Confirm", row: 0, column: 0, route: "post:" },
    { action: "edit", label: "Edit", row: 1, column: 0, route: "edit:" },
    { action: "reject", label: "Reject", row: 1, column: 1, route: "reject:" },
  ] as const;
  const buttons: TelegramInteractiveButtons = [[], []];
  for (let index = 0; index < expected.length; index += 1) {
    const control = controls[index];
    const contract = expected[index]!;
    if (!isRecord(control) || control.action !== contract.action ||
        control.label !== contract.label || control.row_index !== contract.row ||
        control.column_index !== contract.column ||
        typeof control.callback_value !== "string" ||
        !control.callback_value.startsWith(contract.route) ||
        !/^fha1_[A-Za-z0-9_-]{24}$/u.test(control.callback_value.slice(contract.route.length))) {
      throw new Error("D2 edited delivery control is invalid.");
    }
    buttons[contract.row]!.push({
      text: contract.label,
      callback_data: control.callback_value,
    });
  }
  return {
    attemptNonce: posting.delivery_attempt_nonce,
    buttons,
    text: posting.text,
  };
}

function requireIncompleteRejectButton(
  result: JsonObject,
  card: RedeemedDraftCard,
): TelegramInteractiveButtons {
  const actions = result.actions;
  if (result.proposal_public_id !== card.proposalPublicId ||
      result.proposal_version !== card.proposalVersion ||
      result.content_hash !== card.proposalContentHash ||
      result.card_generation_public_id !== card.cardReference ||
      result.final_transaction_created !== false || !isRecord(actions) ||
      Object.keys(actions).length !== 1 || !isRecord(actions.reject) ||
      typeof actions.reject.reference !== "string" ||
      !/^fha1_[A-Za-z0-9_-]{24}$/u.test(actions.reject.reference) ||
      typeof actions.reject.expiry !== "number" || !Number.isSafeInteger(actions.reject.expiry)) {
    throw new Error("Incomplete D1 Reject action is invalid.");
  }
  return [[{
    text: "Reject",
    callback_data: humanActionCallbackData("reject", actions.reject.reference),
    style: "danger",
  }]];
}

function redeemedD1Reference(
  material: JsonObject,
  context: ActiveContext,
  binding: PluginConversationBinding,
): string | undefined {
  const value = material.d1_decision_binding;
  if (value === undefined) return undefined;
  if (!isRecord(value) || Object.keys(value).sort().join(",") !==
      "authenticated_actor_id,card_generation_public_id,conversation_binding_id,reference_public_id,telegram_account_id,telegram_conversation_id" ||
      typeof value.reference_public_id !== "string" ||
      !/^haref_[0-9a-f]{32}$/u.test(value.reference_public_id) ||
      typeof value.card_generation_public_id !== "string" ||
      !/^d1card_[0-9a-f]{32}$/u.test(value.card_generation_public_id) ||
      value.authenticated_actor_id !== context.senderId ||
      value.telegram_account_id !== context.accountId ||
      value.telegram_conversation_id !== context.conversationId ||
      value.conversation_binding_id !== binding.bindingId) {
    throw new Error("Redeemed D1 decision binding is invalid.");
  }
  return value.reference_public_id;
}

interface PostingStatusResult {
  state: "awaiting_confirmation" | "posting" | "finalized" | "rejected" | "needs_attention";
  attemptPublicId?: string;
  transactionPublicId?: string;
  amount?: string;
  currency?: string;
  transactionDate?: string;
  merchant?: string;
}

function requirePostingStatus(result: JsonObject): PostingStatusResult {
  const states = new Set([
    "awaiting_confirmation", "posting", "finalized", "rejected", "needs_attention",
  ]);
  if (typeof result.review_public_id !== "string" ||
      !/^d2rev_[0-9a-f]{30}$/u.test(result.review_public_id) ||
      typeof result.state !== "string" || !states.has(result.state) ||
      (result.attempt_public_id !== null &&
       (typeof result.attempt_public_id !== "string" ||
        !/^d2att_[0-9a-f]{30}$/u.test(result.attempt_public_id))) ||
      (result.transaction_public_id !== null &&
       (typeof result.transaction_public_id !== "string" ||
        !/^[A-Za-z0-9_-]{8,200}$/u.test(result.transaction_public_id))) ||
      (result.attention_reason !== null && typeof result.attention_reason !== "string") ||
      typeof result.final_transaction_created !== "boolean") {
    throw new Error("D2 posting status is invalid.");
  }
  const finalized = result.state === "finalized";
  if (finalized !== result.final_transaction_created ||
      finalized !== (result.transaction_public_id !== null) ||
      (finalized && result.attempt_public_id === null)) {
    throw new Error("D2 final transaction status is inconsistent.");
  }
  if (!finalized) {
    return {
      state: result.state as PostingStatusResult["state"],
      ...(typeof result.attempt_public_id === "string"
        ? { attemptPublicId: result.attempt_public_id } : {}),
    };
  }
  const fields = ["amount", "currency", "transaction_date", "merchant"] as const;
  if (result.account !== "unspecified" ||
      fields.some((field) => typeof result[field] !== "string" ||
        Buffer.byteLength(result[field] as string, "utf8") > 1_024 ||
        /[\p{C}\p{Zl}\p{Zp}]/u.test(result[field] as string))) {
    throw new Error("D2 final transaction projection is invalid.");
  }
  return {
    state: "finalized",
    attemptPublicId: result.attempt_public_id as string,
    transactionPublicId: result.transaction_public_id as string,
    amount: result.amount as string,
    currency: result.currency as string,
    transactionDate: result.transaction_date as string,
    merchant: result.merchant as string,
  };
}

function renderPostingSuccess(status: PostingStatusResult): string {
  if (status.state !== "finalized" || status.transactionPublicId === undefined ||
      status.amount === undefined || status.currency === undefined ||
      status.transactionDate === undefined || status.merchant === undefined) {
    throw new Error("D2 posting result is not final.");
  }
  return [
    "Finance posted.",
    `Transaction ID: ${status.transactionPublicId}`,
    `Amount: ${status.amount} ${status.currency}`,
    `Date: ${status.transactionDate}`,
    `Merchant: ${status.merchant || "not provided"}`,
    "Account: Not specified",
  ].join("\n");
}

async function handlePostingAction(
  context: ActiveContext,
  current: HumanActionRuntime,
  binding: PluginConversationBinding,
): Promise<{ handled: true }> {
  const commandContext = {
    workspace_path: current.workspaceRoot,
    operator_actor_id: context.senderId,
    telegram_account_id: context.accountId,
    telegram_conversation_id: context.conversationId,
    conversation_binding_id: binding.bindingId,
  };
  const confirm = async (): Promise<PostingStatusResult | undefined> => {
    try {
      const response = await current.runner.run(createBridgeRequest(
        "confirm_and_post",
        {
          ...commandContext,
          short_reference: context.reference,
          callback_id: context.callbackId,
          callback_message_id: context.callbackMessageId,
        },
        postingConfirmationKey(context.callbackId),
      ), 30_000);
      return response.status === "ok" ? requirePostingStatus(response.result) : undefined;
    } catch {
      return undefined;
    }
  };
  const query = async (): Promise<PostingStatusResult | undefined> => {
    try {
      const response = await current.runner.run(createBridgeRequest(
        "get_status",
        { ...commandContext, short_reference: context.reference },
      ), 30_000);
      return response.status === "ok" ? requirePostingStatus(response.result) : undefined;
    } catch {
      return undefined;
    }
  };
  const resume = async (attemptPublicId: string): Promise<PostingStatusResult | undefined> => {
    try {
      const response = await current.runner.run(createBridgeRequest(
        "resume_posting",
        { ...commandContext, attempt_public_id: attemptPublicId },
        postingResumeKey(attemptPublicId),
      ), 30_000);
      return response.status === "ok" ? requirePostingStatus(response.result) : undefined;
    } catch {
      return undefined;
    }
  };

  let status = await confirm();
  if (status === undefined) status = await query();
  if (status?.state === "awaiting_confirmation") {
    status = await confirm();
    if (status === undefined) status = await query();
  }
  if (status?.state === "posting" && status.attemptPublicId !== undefined) {
    status = await resume(status.attemptPublicId);
    if (status === undefined) status = await query();
  }
  if (status?.state === "finalized") {
    const currentBinding = await context.currentBinding().catch(() => null);
    if (currentBinding === null || currentBinding.bindingId !== binding.bindingId ||
        !activeBindingMatches(context, currentBinding)) return { handled: true };
    const rendered = renderPostingSuccess(status);
    try {
      await context.replace(rendered, []);
    } catch {
      await context.reply(
        `${rendered}\nThe Telegram review card could not be updated.`,
      ).catch(() => undefined);
    }
    return { handled: true };
  }
  if (status?.state === "needs_attention" || status?.state === "rejected") {
    await context.reply(POSTING_NEEDS_ATTENTION_REPLY).catch(() => undefined);
    return { handled: true };
  }
  await context.reply(POSTING_OUTCOME_UNKNOWN_REPLY).catch(() => undefined);
  return { handled: true };
}

export function createHumanActionInteractiveHandler(
  runtime: () => HumanActionRuntime | undefined,
) {
  return async (value: unknown): Promise<{ handled: true }> => {
    const disabled = narrowContext(value);
    if (disabled !== undefined) {
      try {
        const binding = await disabled.currentBinding();
        if (binding !== null && bindingMatches(disabled, binding)) await disabled.reply(DISABLED_REPLY);
      } catch { /* fail closed */ }
      return { handled: true };
    }
    const context = narrowActiveContext(value);
    if (context === undefined) return { handled: true };
    if (context.action === "post") {
      try {
        const binding = await context.currentBinding();
        const current = runtime();
        if (binding === null || !activeBindingMatches(context, binding) || current === undefined) {
          return { handled: true };
        }
        return await handlePostingAction(context, current, binding);
      } catch {
        await context.reply(POSTING_OUTCOME_UNKNOWN_REPLY).catch(() => undefined);
        return { handled: true };
      }
    }
    let current: HumanActionRuntime;
    let material: JsonObject;
    let proposal: string;
    let decisionKey: string;
    let durableD1Reference: string | undefined;
    let bindingId: string;
    try {
      const binding = await context.currentBinding();
      const available = runtime();
      if (binding === null || !activeBindingMatches(context, binding) || available === undefined) {
        return { handled: true };
      }
      current = available;
      const redemption = await current.runner.run(createBridgeRequest(
        "redeem_human_action",
        {
          workspace_path: current.workspaceRoot,
          short_reference: context.reference,
          action: context.action,
          operator_actor_id: context.senderId,
          telegram_account_id: context.accountId,
          telegram_conversation_id: context.conversationId,
          conversation_binding_id: binding.bindingId,
          callback_id: context.callbackId,
          callback_message_id: context.callbackMessageId,
        },
        humanActionRedemptionKey(context.callbackId),
      ), 30_000);
      if (redemption.status !== "ok") throw new Error("Human action redemption refused.");
      material = redemption.result;
      if (requireString(material, "action") !== context.action ||
          requireString(material, "operator_actor_id") !== context.senderId ||
          material.final_transaction_created !== false) {
        throw new Error("Human action redemption identity mismatch.");
      }
      const bindingAfterRedemption = await context.currentBinding();
      if (bindingAfterRedemption === null ||
          bindingAfterRedemption.bindingId !== binding.bindingId ||
          !activeBindingMatches(context, bindingAfterRedemption)) {
        throw new Error("Human action binding changed during redemption.");
      }
      proposal = requireString(material, "proposal_public_id");
      decisionKey = requireString(material, "decision_idempotency_key");
      const expectedDecisionKey = context.action === "edit"
        ? `bridge-edit:${proposal}:v${requireInteger(material, "proposal_version")}:` +
          requireString(material, "content_hash")
        : `bridge-${context.action}:${proposal}`;
      if (decisionKey !== expectedDecisionKey) {
        throw new Error("Human action decision identity mismatch.");
      }
      durableD1Reference = redeemedD1Reference(material, context, bindingAfterRedemption);
      bindingId = bindingAfterRedemption.bindingId;
    } catch {
      await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
      return { handled: true };
    }

    if (context.action === "edit") {
      let card: RedeemedDraftCard;
      let buttons: TelegramInteractiveButtons;
      let financeDeliveryAttemptNonce: string | undefined;
      try {
        card = renderRedeemedDraftCard(material);
        const commandContext = {
          workspace_path: current.workspaceRoot,
          operator_actor_id: context.senderId,
          telegram_account_id: context.accountId,
          telegram_conversation_id: context.conversationId,
          conversation_binding_id: bindingId,
        };
        if (card.confirmAvailable) {
          if (context.sessionKey !== bindingId) {
            throw new Error("D2 interactive delivery session does not match the private binding.");
          }
          const prepared = await current.runner.run(createBridgeRequest(
            "prepare_posting_review",
            { ...commandContext, card_generation_public_id: card.cardReference },
            postingReviewPreparationKey(card.cardReference),
          ), 30_000);
          if (prepared.status !== "ok") {
            throw new Error("D2 edited posting review was refused.");
          }
          const review = requireRedeemedPostingReview(prepared.result, card);
          card = { ...card, text: review.text };
          const posting = await current.runner.run(createBridgeRequest(
            "issue_posting_review_actions",
            { ...commandContext, posting_review_public_id: review.reviewPublicId },
            postingActionIssuanceKey(review.reviewPublicId),
          ), 30_000);
          if (posting.status !== "ok") {
            throw new Error("D2 edited delivery manifest was refused.");
          }
          const delivery = requireRedeemedD2Buttons(
            posting.result, card, review.reviewPublicId,
          );
          buttons = delivery.buttons;
          card = { ...card, text: delivery.text };
          financeDeliveryAttemptNonce = delivery.attemptNonce;
        } else {
          const issued = await current.runner.run(createBridgeRequest(
            "issue_human_actions",
            {
              ...commandContext,
              proposal_public_id: card.proposalPublicId,
              reference_batch_id: card.actionIssueBatchId,
              token_ttl_seconds: 600,
              expected_proposal_version: card.proposalVersion,
              expected_content_hash: card.proposalContentHash,
              card_generation_public_id: card.cardReference,
            },
            humanActionIssuanceKey(card.actionIssueBatchId),
          ), 30_000);
          if (issued.status !== "ok") {
            throw new Error("D1 human action issuance was refused.");
          }
          buttons = requireIncompleteRejectButton(issued.result, card);
          const attemptPublicId = humanDraftDeliveryAttemptId(card.cardReference, "replace");
          const begun = await current.runner.run(createBridgeRequest(
            "begin_human_draft_card_delivery",
            {
              ...commandContext,
              card_generation_public_id: card.cardReference,
              attempt_public_id: attemptPublicId,
              delivery_material_hash: framedDigest(
                "d1-card-delivery-material-v1",
                card.text,
                ...buttons.flat().map((button) => button.callback_data),
              ),
              transport_mode: "replace",
              outbound_target_message_id: String(context.callbackMessageId),
            },
            humanDraftDeliveryKey(attemptPublicId),
          ), 30_000);
          if (begun.status !== "ok" || begun.result.attempt_public_id !== attemptPublicId) {
            throw new Error("Human draft replacement attempt was not persisted.");
          }
          const observationPublicId = humanDraftObservationId(attemptPublicId, "initial");
          const observed = await current.runner.run(createBridgeRequest(
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
          ), 30_000);
          if (observed.status !== "ok" ||
              observed.result.observation_public_id !== observationPublicId) {
            throw new Error("Human draft replacement observation was not persisted.");
          }
        }
      } catch {
        await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
        return { handled: true };
      }
      try {
        await context.replace(card.text, buttons, financeDeliveryAttemptNonce);
      } catch {
        await context.reply(EDIT_PRESENTATION_FAILURE_REPLY).catch(() => undefined);
      }
      return { handled: true };
    }

    let decision: BridgeResponse;
    try {
      decision = await current.runner.run(createBridgeRequest(
        context.action,
        {
          workspace_path: current.workspaceRoot,
          proposal_public_id: proposal,
          operator_actor_id: context.senderId,
          proposal_version: requireInteger(material, "proposal_version"),
          content_hash: requireString(material, "content_hash"),
          callback_token: requireString(material, "callback_token"),
          callback_expiry: requireInteger(material, "callback_expiry"),
          ...(durableD1Reference === undefined
            ? {}
            : { d1_reference_public_id: durableD1Reference }),
        },
        decisionKey,
      ), 30_000);
    } catch {
      await context.reply(ACTION_OUTCOME_UNKNOWN_REPLY).catch(() => undefined);
      return { handled: true };
    }
    if (decision.status !== "ok") {
      await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
      return { handled: true };
    }
    try {
      if (decision.result.proposal_public_id !== proposal ||
          decision.result.decision !== context.action + "ed" ||
          decision.result.final_transaction_created !== false) {
        throw new Error("Human action decision refused.");
      }
    } catch {
      await context.reply(ACTION_OUTCOME_UNKNOWN_REPLY).catch(() => undefined);
      return { handled: true };
    }

    const persistedReply = context.action === "confirm"
      ? "Finance proposal confirmed. Finalization has not run."
      : "Finance proposal rejected. No final transaction was created.";
    try {
      await context.replace(persistedReply, []);
    } catch {
      await context.reply(
        `${persistedReply} The Telegram review card could not be updated.`,
      ).catch(() => undefined);
    }
    return { handled: true };
  };
}

export function createDisabledInteractiveHandler(_privateController: () => void) {
  return async (value: unknown): Promise<{ handled: true }> => {
    const context = narrowContext(value);
    if (context === undefined) return { handled: true };
    try {
      const binding = await context.currentBinding();
      if (binding === null || !bindingMatches(context, binding)) return { handled: true };
      await context.reply(DISABLED_REPLY);
    } catch {
      return { handled: true };
    }
    return { handled: true };
  };
}
