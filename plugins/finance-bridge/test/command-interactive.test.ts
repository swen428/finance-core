import assert from "node:assert/strict";
import test from "node:test";

import type {
  PluginCommandContext,
  PluginConversationBinding,
} from "openclaw-sdk/plugin-sdk/plugin-entry";
import { createFinanceCommand } from "../src/command.js";
import {
  ACTION_FAILURE_REPLY,
  ACTION_OUTCOME_UNKNOWN_REPLY,
  ACTIVE_ACTIONS,
  DISABLED_ACTIONS,
  DISABLED_REPLY,
  EDIT_PRESENTATION_FAILURE_REPLY,
  createHumanActionInteractiveHandler,
  createDisabledInteractiveHandler,
  disabledCallbackData,
  humanActionCallbackData,
} from "../src/interactive.js";
import {
  framedDigest,
  type BridgeRequest,
  type BridgeResponse,
  type JsonObject,
} from "../src/protocol.js";

const binding: PluginConversationBinding = {
  bindingId: "binding-1",
  pluginId: "finance-bridge",
  pluginRoot: "/plugin",
  channel: "telegram",
  accountId: "finance-account",
  conversationId: "111",
  parentConversationId: "111",
  boundAt: 1,
  data: { senderId: "111" },
};

function commandContext(
  subcommand: string,
  overrides: Partial<PluginCommandContext> = {},
): PluginCommandContext {
  return {
    channel: "telegram",
    isAuthorizedSender: true,
    senderIsOwner: true,
    senderId: "111",
    accountId: "finance-account",
    from: "telegram:111",
    to: "telegram:111",
    commandBody: `/finance ${subcommand}`,
    args: subcommand,
    config: {},
    async requestConversationBinding() { return { status: "bound", binding }; },
    async detachConversationBinding() { return { removed: true }; },
    async getCurrentConversationBinding() { return binding; },
    ...overrides,
  };
}

test("finance bind, status, and unbind use only core binding methods", async () => {
  let privateCalls = 0;
  const command = createFinanceCommand(() => {
    privateCalls += 1;
    return true;
  });
  assert.deepEqual(
    {
      name: command.name,
      channels: command.channels,
      acceptsArgs: command.acceptsArgs,
      requireAuth: command.requireAuth,
      requiredScopes: command.requiredScopes,
      exposeSenderIsOwner: command.exposeSenderIsOwner,
    },
    {
      name: "finance",
      channels: ["telegram"],
      acceptsArgs: true,
      requireAuth: true,
      requiredScopes: ["operator.write"],
      exposeSenderIsOwner: true,
    },
  );

  const bound = await command.handler(commandContext("bind"));
  assert.match(bound.text ?? "", /Bound this private conversation/u);
  assert.equal(bound.continueAgent, false);
  const status = await command.handler(commandContext("status"));
  assert.equal(status.text, "Finance bridge is bound to this private conversation.");
  const detached = await command.handler(commandContext("unbind"));
  assert.equal(detached.text, "Finance bridge binding removed.");
  assert.equal(privateCalls, 3);
});

test("finance command refuses non-owner, ambiguous route, pending approval, and unknown args", async () => {
  const command = createFinanceCommand(() => true);
  const cases: PluginCommandContext[] = [
    commandContext("bind", { senderIsOwner: false }),
    commandContext("bind", { messageThreadId: 1 }),
    commandContext("bind", { senderId: "222" }),
    commandContext("bind", { gatewayClientScopes: ["operator.admin"] }),
    commandContext("other"),
  ];
  for (const context of cases) {
    const result = await command.handler(context);
    assert.match(result.text ?? "", /not available|private direct message|bind, status, or unbind/u);
    assert.equal(result.continueAgent, false);
  }
  const pending = await command.handler(commandContext("bind", {
    async requestConversationBinding() {
      return { status: "pending", approvalId: "approval-1", reply: { text: "Approval required." } };
    },
  }));
  assert.deepEqual(pending, { text: "Approval required.", continueAgent: false });

  for (const subcommand of ["bind", "status", "unbind"]) {
    const methods = {
      async requestConversationBinding(): Promise<never> { throw new Error("secret host failure"); },
      async getCurrentConversationBinding(): Promise<never> { throw new Error("secret host failure"); },
      async detachConversationBinding(): Promise<never> { throw new Error("secret host failure"); },
    };
    const failed = await command.handler(commandContext(subcommand, methods));
    assert.deepEqual(failed, { text: "Finance bridge is not available.", continueAgent: false });
  }
});

test("finance status by intake is private-bound, read-only, and renders durable attribution", async () => {
  const requests: BridgeRequest[] = [];
  const command = createFinanceCommand(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        requests.push(request);
        return {
          envelopeVersion: "v1",
          requestId: request.request_id,
          operationId: "op_0123456789abcdef0123456789abcdef",
          status: "ok",
          result: {
            intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
            attempt_public_id: `aifa_${"a".repeat(64)}`,
            receipt_public_id: `aimr_${"b".repeat(64)}`,
            admission_decision_public_id: null,
            processing_path: "cloud_projection",
            safe_reason_code: null,
            canonical_attribution: {
              provider: "openai", model: "gpt-5.6-luna", agent_id: "finance",
            },
            display_alias: "GPT Luna",
            attribution_match: true,
          },
          idempotentReplay: false,
        };
      },
    },
  }));
  const result = await command.handler(commandContext(
    "status raw_intake_12345678-1234-1234-1234-123456789abc",
  ));
  assert.match(result.text ?? "", /Display: ☁️ GPT Luna/u);
  assert.match(result.text ?? "", /Canonical attribution: openai\/gpt-5\.6-luna/u);
  assert.equal(requests.length, 1);
  assert.equal(requests[0]?.command, "get_ai_processing_status_v2");
  assert.deepEqual(requests[0]?.arguments, {
    workspace_path: "/tmp/workspace",
    intake_public_id: "raw_intake_12345678-1234-1234-1234-123456789abc",
  });

  const unbound = await command.handler(commandContext(
    "status raw_intake_12345678-1234-1234-1234-123456789abc",
    { async getCurrentConversationBinding() { return null; } },
  ));
  assert.equal(unbound.text, "Finance bridge is not bound to this private conversation.");
  assert.equal(requests.length, 1);
});

test("disabled callbacks are fixed, bounded, binding-checked, and never submit text or call CLI", async () => {
  let privateCalls = 0;
  let replies = 0;
  const handler = createDisabledInteractiveHandler(() => {
    privateCalls += 1;
  });
  assert.deepEqual(DISABLED_ACTIONS, ["edit-disabled"]);
  for (const action of DISABLED_ACTIONS) {
    const data = disabledCallbackData(action);
    assert.ok(Buffer.byteLength(data, "utf8") <= 64);
    assert.equal(data, `finance-bridge:${action}`);
    const result = await handler({
      channel: "telegram",
      accountId: "finance-account",
      conversationId: "111",
      parentConversationId: "111",
      senderId: "111",
      isGroup: false,
      isForum: false,
      auth: { isAuthorizedSender: true },
      callback: {
        data,
        namespace: "finance-bridge",
        payload: action,
        messageId: 20,
        chatId: "111",
      },
      respond: { async reply({ text }: { text: string }) { replies += 1; assert.equal(text, DISABLED_REPLY); } },
      async getCurrentConversationBinding() { return binding; },
    });
    assert.deepEqual(result, { handled: true });
  }
  assert.equal(replies, 1);
  assert.equal(privateCalls, 0);

  for (const candidate of [
    null,
    {},
    { channel: "telegram", callback: { namespace: "finance-bridge", payload: "confirm" } },
    {
      channel: "telegram",
      accountId: "finance-account",
      conversationId: "111",
      senderId: "111",
      isGroup: false,
      isForum: false,
      threadId: 1,
      auth: { isAuthorizedSender: true },
      callback: {
        data: disabledCallbackData("edit-disabled"),
        namespace: "finance-bridge",
        payload: "edit-disabled",
        messageId: 20,
        chatId: "111",
      },
    },
  ]) {
    assert.deepEqual(await handler(candidate), { handled: true });
  }
});

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

test("active direct-human callbacks redeem durably then decide without submitText or finalization", async () => {
  assert.deepEqual(ACTIVE_ACTIONS, ["confirm", "edit", "reject"]);
  const reference = `fha1_${"A".repeat(24)}`;
  const requests: BridgeRequest[] = [];
  const edits: string[] = [];
  let replies = 0;
  let bindingReads = 0;
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        requests.push(request);
        if (request.command === "redeem_human_action") {
          return ok(request, {
            action: "confirm",
            proposal_public_id: "prop_bridge_0123456789abcdef0123456789abcdef",
            operator_actor_id: "111",
            proposal_version: 0,
            content_hash: "1".repeat(64),
            callback_token: `fcb_v1_${"A".repeat(32)}`,
            callback_expiry: 2_000_000_000,
            decision_idempotency_key:
              "bridge-confirm:prop_bridge_0123456789abcdef0123456789abcdef",
            final_transaction_created: false,
          });
        }
        return ok(request, {
          decision: "confirmed",
          proposal_public_id: "prop_bridge_0123456789abcdef0123456789abcdef",
          final_transaction_created: false,
        });
      },
    },
  }));
  const data = humanActionCallbackData("confirm", reference);
  assert.ok(Buffer.byteLength(data, "utf8") <= 64);
  const result = await handler({
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-1",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data,
      namespace: "finance-bridge",
      payload: `confirm:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply() { replies += 1; },
      async editMessage({ text }: {text: string}) { edits.push(text); },
    },
    async getCurrentConversationBinding() {
      bindingReads += 1;
      return binding;
    },
  });
  assert.deepEqual(result, { handled: true });
  assert.deepEqual(requests.map((request) => request.command), [
    "redeem_human_action", "confirm",
  ]);
  assert.equal(requests[0]?.idempotency_key,
    "bridge-human-action-redeem:9ebecace6ca2e39d2e32e183e555ccb8");
  assert.equal(requests[1]?.idempotency_key,
    "bridge-confirm:prop_bridge_0123456789abcdef0123456789abcdef");
  assert.equal(replies, 0);
  assert.equal(bindingReads, 2);
  assert.deepEqual(edits, ["Finance proposal confirmed. Finalization has not run."]);
  assert.equal("submitText" in result, false);
});

test("generation-bound decisions carry only the redeemed durable D1 reference", async () => {
  const reference = `fha1_${"H".repeat(24)}`;
  const proposal = `po_d1_${"1".repeat(32)}`;
  const generation = `d1card_${"2".repeat(32)}`;
  const durableReference = `haref_${"3".repeat(32)}`;
  const requests: BridgeRequest[] = [];
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        requests.push(request);
        if (request.command === "redeem_human_action") {
          return ok(request, {
            action: "confirm",
            proposal_public_id: proposal,
            operator_actor_id: "111",
            proposal_version: 0,
            content_hash: "4".repeat(64),
            callback_token: `fcb_v1_${"A".repeat(32)}`,
            callback_expiry: 2_000_000_000,
            decision_idempotency_key: `bridge-confirm:${proposal}`,
            d1_decision_binding: {
              reference_public_id: durableReference,
              card_generation_public_id: generation,
              authenticated_actor_id: "111",
              telegram_account_id: "finance-account",
              telegram_conversation_id: "111",
              conversation_binding_id: "binding-1",
            },
            final_transaction_created: false,
          });
        }
        return ok(request, {
          decision: "confirmed",
          proposal_public_id: proposal,
          final_transaction_created: false,
        });
      },
    },
  }));
  await handler({
    channel: "telegram", accountId: "finance-account", callbackId: "callback-d1-confirm",
    conversationId: "111", parentConversationId: "111", senderId: "111",
    isGroup: false, isForum: false, auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("confirm", reference), namespace: "finance-bridge",
      payload: `confirm:${reference}`, messageId: 20, chatId: "111",
    },
    respond: { async reply() {}, async editMessage() {} },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.deepEqual(requests.map((request) => request.command), ["redeem_human_action", "confirm"]);
  assert.equal(requests[1]?.arguments.d1_reference_public_id, durableReference);
  assert.equal("card_generation_public_id" in (requests[1]?.arguments ?? {}), false);
});

test("edit callback renders the atomically started D1 card without a second begin command", async () => {
  const reference = `fha1_${"G".repeat(24)}`;
  const requests: BridgeRequest[] = [];
  const edits: Array<{
    text: string;
    buttons: Array<Array<{text: string; callback_data: string; style?: string}>>;
  }> = [];
  const proposal = "prop_bridge_0123456789abcdef0123456789abcdef";
  const cardReference = `d1card_${"2".repeat(32)}`;
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        requests.push(request);
        if (request.command === "begin_human_draft_card_delivery") {
          return ok(request, { attempt_public_id: request.arguments.attempt_public_id! });
        }
        if (request.command === "record_human_draft_card_delivery_outcome") {
          return ok(request, { observation_public_id: request.arguments.observation_public_id! });
        }
        if (request.command === "issue_human_actions") {
          return ok(request, {
            proposal_public_id: proposal,
            proposal_version: 0,
            content_hash: "1".repeat(64),
            card_generation_public_id: cardReference,
            actions: {
              reject: { reference: `fha1_${"R".repeat(24)}`, expiry: 2_000_000_000 },
            },
            final_transaction_created: false,
          });
        }
        return ok(request, {
          action: "edit",
          proposal_public_id: proposal,
          operator_actor_id: "111",
          proposal_version: 0,
          content_hash: "1".repeat(64),
          callback_token: `fcb_v1_${"A".repeat(32)}`,
          callback_expiry: 2_000_000_000,
          decision_idempotency_key: `bridge-edit:${proposal}:v0:${"1".repeat(64)}`,
          human_draft_card: {
            draft_public_id: `d1draft_${"3".repeat(32)}`,
            draft_version: 0,
            draft_content_hash: "4".repeat(64),
            completeness: "incomplete",
            reason_contributors: [],
            unresolved_flags: ["missing_amount"],
            human_reply_evidence_public_id: null,
            delivery_state: "not_attempted",
            delivery_state_hash: "5".repeat(64),
            delivery_attempts: [],
            delivery_outcomes: [],
            action_issue_batch_id: "6".repeat(64),
            operation_outcome: "started",
            refusal_code: null,
            idempotent_replay: false,
            action_issuance_state: "not_issued",
            proposal_public_id: null,
            proposal_version: null,
            proposal_content_hash: null,
            card_generation_public_id: cardReference,
            current_card_generation_public_id: cardReference,
            original_operation_or_start_public_id: `d1start_${"7".repeat(32)}`,
            field_values: {
              amount: "", currency: "SGD", transaction_date: "2026-09-19",
              merchant: "Example", description: "Lunch", category: "Meals",
            },
            decision_target_proposal_public_id: proposal,
            decision_target_proposal_version: 0,
            decision_target_proposal_content_hash: "1".repeat(64),
            confirm_available: false,
            reject_available: true,
            final_transaction_created: false,
          },
          final_transaction_created: false,
        });
      },
    },
  }));
  await handler({
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-guided-edit",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("edit", reference),
      namespace: "finance-bridge",
      payload: `edit:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply() { throw new Error("replacement should succeed"); },
      async editMessage(params: {
        text: string;
        buttons: Array<Array<{text: string; callback_data: string; style?: string}>>;
      }) { edits.push(params); },
    },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.deepEqual(requests.map((request) => request.command), [
    "redeem_human_action",
    "issue_human_actions",
    "begin_human_draft_card_delivery",
    "record_human_draft_card_delivery_outcome",
  ]);
  assert.match(edits[0]?.text ?? "", new RegExp(`Card Ref: ${cardReference}`, "u"));
  assert.match(edits[0]?.text ?? "", /Status: incomplete/u);
  assert.match(edits[0]?.text ?? "", /Amount: \n/u);
  assert.doesNotMatch(edits[0]?.text ?? "", /Reply 完成|field=/u);
  assert.deepEqual(edits[0]?.buttons, [[{
    text: "Reject",
    callback_data: humanActionCallbackData("reject", `fha1_${"R".repeat(24)}`),
    style: "danger",
  }]]);
  const firstDelivery = requests.find(
    (request) => request.command === "begin_human_draft_card_delivery",
  );
  assert.equal(firstDelivery?.arguments.delivery_material_hash, framedDigest(
    "d1-card-delivery-material-v1",
    edits[0]?.text ?? "",
    humanActionCallbackData("reject", `fha1_${"R".repeat(24)}`),
  ));
  assert.notEqual(firstDelivery?.arguments.delivery_material_hash, framedDigest(
    "d1-card-delivery-material-v1",
    edits[0]?.text ?? "",
  ));

  const failureReplies: string[] = [];
  await handler({
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-guided-edit-replacement-failure",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("edit", reference),
      namespace: "finance-bridge",
      payload: `edit:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply({ text }: {text: string}) { failureReplies.push(text); },
      async editMessage() { throw new Error("synthetic D1 replacement failure"); },
    },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.deepEqual(requests.slice(-4).map((request) => request.command), [
    "redeem_human_action",
    "issue_human_actions",
    "begin_human_draft_card_delivery",
    "record_human_draft_card_delivery_outcome",
  ]);
  assert.deepEqual(failureReplies, [EDIT_PRESENTATION_FAILURE_REPLY]);
  assert.doesNotMatch(failureReplies[0] ?? "", new RegExp(cardReference, "u"));
});

test("editing a current complete D1 card removes the consumed Edit control", async () => {
  const reference = `fha1_${"E".repeat(24)}`;
  const proposal = `po_d1_${"1".repeat(32)}`;
  const cardReference = `d1card_${"2".repeat(32)}`;
  const actionReferences = {
    confirm: `fha1_${"C".repeat(24)}`,
    edit: reference,
    reject: `fha1_${"R".repeat(24)}`,
  };
  const edits: Array<Array<Array<{text: string; callback_data: string}>>> = [];
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        if (request.command === "issue_human_actions") {
          return ok(request, {
            proposal_public_id: proposal,
            proposal_version: 0,
            content_hash: "3".repeat(64),
            card_generation_public_id: cardReference,
            actions: {
              confirm: { reference: actionReferences.confirm, expiry: 2_000_000_000 },
              edit: { reference: actionReferences.edit, expiry: 2_000_000_000 },
              reject: { reference: actionReferences.reject, expiry: 2_000_000_000 },
            },
            final_transaction_created: false,
          });
        }
        if (request.command === "begin_human_draft_card_delivery") {
          return ok(request, { attempt_public_id: request.arguments.attempt_public_id! });
        }
        if (request.command === "record_human_draft_card_delivery_outcome") {
          return ok(request, { observation_public_id: request.arguments.observation_public_id! });
        }
        return ok(request, {
          action: "edit",
          proposal_public_id: proposal,
          operator_actor_id: "111",
          proposal_version: 0,
          content_hash: "3".repeat(64),
          callback_token: `fcb_v1_${"A".repeat(32)}`,
          callback_expiry: 2_000_000_000,
          decision_idempotency_key: `bridge-edit:${proposal}:v0:${"3".repeat(64)}`,
          d1_decision_binding: {
            reference_public_id: `haref_${"4".repeat(32)}`,
            card_generation_public_id: cardReference,
            authenticated_actor_id: "111",
            telegram_account_id: "finance-account",
            telegram_conversation_id: "111",
            conversation_binding_id: "binding-1",
          },
          human_draft_card: {
            card_generation_public_id: cardReference,
            current_card_generation_public_id: cardReference,
            completeness: "complete",
            confirm_available: true,
            reject_available: true,
            action_issue_batch_id: "5".repeat(64),
            operation_outcome: "accepted",
            refusal_code: null,
            action_issuance_state: "issued",
            proposal_public_id: proposal,
            proposal_version: 0,
            proposal_content_hash: "3".repeat(64),
            decision_target_proposal_public_id: proposal,
            decision_target_proposal_version: 0,
            decision_target_proposal_content_hash: "3".repeat(64),
            field_values: {
              amount: "12.50", currency: "SGD", transaction_date: "2026-09-19",
              merchant: "Example", description: "Lunch", category: "Meals",
            },
            unresolved_flags: [],
            final_transaction_created: false,
          },
          final_transaction_created: false,
        });
      },
    },
  }));
  await handler({
    channel: "telegram", accountId: "finance-account", callbackId: "callback-d1-edit",
    conversationId: "111", parentConversationId: "111", senderId: "111",
    isGroup: false, isForum: false, auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("edit", reference), namespace: "finance-bridge",
      payload: `edit:${reference}`, messageId: 20, chatId: "111",
    },
    respond: {
      async reply() { throw new Error("replacement should succeed"); },
      async editMessage({ buttons }: {
        buttons: Array<Array<{text: string; callback_data: string}>>;
      }) { edits.push(buttons); },
    },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.deepEqual(edits[0]?.[0]?.map((button) => button.text), ["Confirm", "Reject"]);
  assert.equal(
    edits[0]?.flat().some((button) => button.callback_data.includes(actionReferences.edit)),
    false,
  );
});

test("persisted decision remains truthful when Telegram card replacement fails", async () => {
  const reference = `fha1_${"D".repeat(24)}`;
  const replies: string[] = [];
  const calls: string[] = [];
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        calls.push(request.command);
        if (request.command === "redeem_human_action") {
          return ok(request, {
            action: "confirm",
            proposal_public_id: "prop_bridge_0123456789abcdef0123456789abcdef",
            operator_actor_id: "111",
            proposal_version: 0,
            content_hash: "1".repeat(64),
            callback_token: `fcb_v1_${"A".repeat(32)}`,
            callback_expiry: 2_000_000_000,
            decision_idempotency_key:
              "bridge-confirm:prop_bridge_0123456789abcdef0123456789abcdef",
            final_transaction_created: false,
          });
        }
        return ok(request, {
          decision: "confirmed",
          proposal_public_id: "prop_bridge_0123456789abcdef0123456789abcdef",
          final_transaction_created: false,
        });
      },
    },
  }));
  await handler({
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-presentation-failure",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("confirm", reference),
      namespace: "finance-bridge",
      payload: `confirm:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply({ text }: {text: string}) { replies.push(text); },
      async editMessage() { throw new Error("synthetic Telegram presentation failure"); },
    },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.deepEqual(calls, ["redeem_human_action", "confirm"]);
  assert.deepEqual(replies, [
    "Finance proposal confirmed. Finalization has not run. " +
      "The Telegram review card could not be updated.",
  ]);
  assert.notEqual(replies[0], ACTION_FAILURE_REPLY);
});

test("lost decision response reports unknown outcome instead of claiming failure", async () => {
  const reference = `fha1_${"E".repeat(24)}`;
  const replies: string[] = [];
  let calls = 0;
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        calls += 1;
        if (request.command !== "redeem_human_action") {
          throw new Error("synthetic lost decision response");
        }
        return ok(request, {
          action: "reject",
          proposal_public_id: "prop_bridge_0123456789abcdef0123456789abcdef",
          operator_actor_id: "111",
          proposal_version: 0,
          content_hash: "1".repeat(64),
          callback_token: `fcb_v1_${"A".repeat(32)}`,
          callback_expiry: 2_000_000_000,
          decision_idempotency_key:
            "bridge-reject:prop_bridge_0123456789abcdef0123456789abcdef",
          final_transaction_created: false,
        });
      },
    },
  }));
  await handler({
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-lost-response",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data: humanActionCallbackData("reject", reference),
      namespace: "finance-bridge",
      payload: `reject:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply({ text }: {text: string}) { replies.push(text); },
      async editMessage() { throw new Error("must not update unknown outcome"); },
    },
    async getCurrentConversationBinding() { return binding; },
  });
  assert.equal(calls, 2);
  assert.deepEqual(replies, [ACTION_OUTCOME_UNKNOWN_REPLY]);
  assert.notEqual(replies[0], ACTION_FAILURE_REPLY);
});

test("active callbacks fail closed on route, binding, runtime, or CLI refusal", async () => {
  const reference = `fha1_${"B".repeat(24)}`;
  const data = humanActionCallbackData("reject", reference);
  let calls = 0;
  let failureReply = "";
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        calls += 1;
        return {
          envelopeVersion: "v1",
          requestId: request.request_id,
          operationId: null,
          status: "error",
          error: { code: "REFUSED", message: "secret", retryable: false },
        };
      },
    },
  }));
  const base = {
    channel: "telegram",
    accountId: "finance-account",
    callbackId: "callback-2",
    conversationId: "111",
    parentConversationId: "111",
    senderId: "111",
    isGroup: false,
    isForum: false,
    auth: { isAuthorizedSender: true },
    callback: {
      data,
      namespace: "finance-bridge",
      payload: `reject:${reference}`,
      messageId: 20,
      chatId: "111",
    },
    respond: {
      async reply({ text }: {text: string}) { failureReply = text; },
      async editMessage() { throw new Error("unreachable"); },
    },
    async getCurrentConversationBinding() { return binding; },
  };
  assert.deepEqual(await handler({ ...base, auth: { isAuthorizedSender: false } }), {
    handled: true,
  });
  assert.equal(calls, 0);
  assert.deepEqual(await handler(base), { handled: true });
  assert.equal(calls, 1);
  assert.equal(failureReply, ACTION_FAILURE_REPLY);

  const mismatch = createHumanActionInteractiveHandler(() => {
    calls += 1;
    throw new Error("runtime must not be reached");
  });
  assert.deepEqual(await mismatch({
    ...base,
    async getCurrentConversationBinding() {
      return { ...binding, conversationId: "222" };
    },
  }), { handled: true });
});

test("active callbacks reject malformed Telegram and binding identities before CLI", async () => {
  const reference = `fha1_${"C".repeat(24)}`;
  let calls = 0;
  const handler = createHumanActionInteractiveHandler(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(): Promise<BridgeResponse> {
        calls += 1;
        throw new Error("malformed identity must not reach the Finance CLI");
      },
    },
  }));
  const malformed = [
    { actorId: "abc", accountId: "finance-account", bindingId: "binding-1" },
    { actorId: "01", accountId: "finance-account", bindingId: "binding-1" },
    { actorId: "-1", accountId: "finance-account", bindingId: "binding-1" },
    { actorId: "", accountId: "finance-account", bindingId: "binding-1" },
    { actorId: "1".repeat(33), accountId: "finance-account", bindingId: "binding-1" },
    { actorId: "111", accountId: "", bindingId: "binding-1" },
    { actorId: "111", accountId: "finance account", bindingId: "binding-1" },
    { actorId: "111", accountId: "finance\naccount", bindingId: "binding-1" },
    { actorId: "111", accountId: "A".repeat(201), bindingId: "binding-1" },
    { actorId: "111", accountId: "finance-account", bindingId: "" },
    { actorId: "111", accountId: "finance-account", bindingId: "binding id" },
    { actorId: "111", accountId: "finance-account", bindingId: "binding\nid" },
    { actorId: "111", accountId: "finance-account", bindingId: "B".repeat(201) },
  ];
  for (const candidate of malformed) {
    const candidateBinding: PluginConversationBinding = {
      ...binding,
      bindingId: candidate.bindingId,
      accountId: candidate.accountId,
      conversationId: candidate.actorId,
      parentConversationId: candidate.actorId,
      data: { senderId: candidate.actorId },
    };
    assert.deepEqual(await handler({
      channel: "telegram",
      accountId: candidate.accountId,
      callbackId: "callback-malformed-identity",
      conversationId: candidate.actorId,
      parentConversationId: candidate.actorId,
      senderId: candidate.actorId,
      isGroup: false,
      isForum: false,
      auth: { isAuthorizedSender: true },
      callback: {
        data: humanActionCallbackData("confirm", reference),
        namespace: "finance-bridge",
        payload: `confirm:${reference}`,
        messageId: 21,
        chatId: candidate.actorId,
      },
      respond: {
        async reply() { throw new Error("malformed route must stay silent"); },
        async editMessage() { throw new Error("malformed route must stay silent"); },
      },
      async getCurrentConversationBinding() { return candidateBinding; },
    }), { handled: true });
  }
  assert.equal(calls, 0);
});
