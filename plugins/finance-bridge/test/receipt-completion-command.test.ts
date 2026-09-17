import assert from "node:assert/strict";
import test from "node:test";

import type {
  PluginCommandContext,
  PluginConversationBinding,
} from "openclaw-sdk/plugin-sdk/plugin-entry";

import { createFinanceCommand } from "../src/command.js";
import type { BridgeRequest, BridgeResponse, JsonObject } from "../src/protocol.js";

const PROPOSAL_ID = "prop_bridge_0123456789abcdef0123456789abcdef";
const CONTENT_HASH = "a".repeat(64);
const SNAPSHOT_HASH = "b".repeat(64);

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
  args: string,
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
    commandBody: `/finance ${args}`,
    args,
    config: {},
    async requestConversationBinding() { return { status: "bound", binding }; },
    async detachConversationBinding() { return { removed: true }; },
    async getCurrentConversationBinding() { return binding; },
    ...overrides,
  };
}

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

function failure(request: BridgeRequest, retryable: boolean): BridgeResponse {
  return {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: "op_0123456789abcdef0123456789abcdef",
    status: "error",
    error: {
      code: "FINALIZATION_LOCKED",
      message: "intentionally not rendered",
      retryable,
    },
  };
}

function statusResult(): JsonObject {
  return {
    identity_kind: "proposal",
    proposal_public_id: PROPOSAL_ID,
    intake_public_id: "raw_1",
    proposal_version: 2,
    effective_content_hash: CONTENT_HASH,
    parse_status: "confirmed",
    conversion_status: "converted",
    finalization_state: "finalized",
    final_transaction_created: true,
    transaction_public_id: "txn_1",
  };
}

test("private receipt commands derive fresh bindings and never expose an Agent route", async () => {
  const requests: BridgeRequest[] = [];
  const command = createFinanceCommand(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        requests.push(request);
        if (request.command === "get_status") return ok(request, statusResult());
        if (request.command === "get_review") throw new Error("direct receipt commands must not read reviews");
        if (request.command === "prepare_receipt_completion") {
          return ok(request, {
            identity_kind: "prepare_receipt_completion",
            proposal_public_id: PROPOSAL_ID,
            receipt_public_id: "rcpt_1",
            conversion_command_public_id: "rpfc_1",
            conversion_result_hash: "c".repeat(64),
            content_hash: CONTENT_HASH,
          });
        }
        if (request.command === "apply_fact_set") {
          return ok(request, {
            identity_kind: "apply_fact_set",
            receipt_public_id: "rcpt_1",
            fact_set_public_id: "riaf_1",
            fact_set_version: 1,
            fact_set_result_hash: "d".repeat(64),
            item_count: 1,
            allocation_count: 1,
          });
        }
        if (request.command === "get_finalization_snapshot_review") {
          return ok(request, {
            identity_kind: "finalization_snapshot_review",
            proposal_public_id: PROPOSAL_ID,
            receipt_public_id: "rcpt_1",
            fact_set_public_id: "riaf_1",
            fact_set_version: 1,
            fact_set_result_hash: "d".repeat(64),
            calculation_snapshot_id: "snap_1",
            calculation_snapshot_hash: SNAPSHOT_HASH,
            currency: "SGD",
            payer_participant_public_id: "ptcp_owner",
            total_paid: "12.34",
            total_to_collect: "0.00",
            participant_shares: { ptcp_owner: "12.34" },
            settlement_obligations: [],
          });
        }
        if (request.command === "authorize_finalization") {
          return ok(request, {
            identity_kind: "authorize_finalization",
            receipt_public_id: "rcpt_1",
            authorization_id: "authz_1",
            authorization_content_hash: "e".repeat(64),
            calculation_snapshot_id: "snap_1",
            calculation_snapshot_hash: SNAPSHOT_HASH,
          });
        }
        if (request.command === "finalize") {
          return ok(request, {
            identity_kind: "finalize",
            path: "receipt",
            proposal_public_id: PROPOSAL_ID,
            confirmation_public_id: "conf_1",
            receipt_public_id: "rcpt_1",
            fact_set_public_id: "riaf_1",
            fact_set_version: 1,
            calculation_snapshot_id: "snap_1",
            calculation_snapshot_hash: SNAPSHOT_HASH,
            authorization_id: "authz_1",
            finalization_public_id: "fin_1",
            transaction_public_id: "txn_1",
            final_transaction_created: true,
            content_hash: CONTENT_HASH,
          });
        }
        throw new Error(`unexpected command ${request.command}`);
      },
    },
  }));

  const commands = [
    `prepare-receipt ${PROPOSAL_ID}`,
    `apply-facts ${PROPOSAL_ID} fact-set.json`,
    `review-snapshot ${PROPOSAL_ID}`,
    `authorize ${PROPOSAL_ID} ${SNAPSHOT_HASH}`,
    `finalize ${PROPOSAL_ID}`,
    `receipt-status ${PROPOSAL_ID}`,
  ];
  const responses = [];
  for (const args of commands) {
    const result = await command.handler(commandContext(args));
    responses.push(result);
    assert.equal(result.continueAgent, false);
    assert.equal("submitText" in result, false);
  }

  assert.match(responses[2]!.text ?? "", /Total paid: 12\.34/u);
  assert.match(responses[0]!.text ?? "", /Receipt rcpt_1/u);
  assert.match(responses[0]!.text ?? "", new RegExp(`Conversion hash: ${"c".repeat(64)}`, "u"));
  assert.match(responses[2]!.text ?? "", /Receipt rcpt_1/u);
  assert.match(responses[2]!.text ?? "", new RegExp(`Review hash: ${SNAPSHOT_HASH}`, "u"));
  assert.equal(responses[5]!.text ?? "", "Receipt status: finalized.");

  assert.deepEqual(requests.map((request) => request.command), [
    "get_status", "prepare_receipt_completion",
    "apply_fact_set",
    "get_finalization_snapshot_review",
    "authorize_finalization",
    "get_status", "finalize",
    "get_status",
  ]);
  for (const request of requests) {
    assert.equal(request.arguments.workspace_path, "/tmp/workspace");
    assert.equal(JSON.stringify(request.arguments).includes("12.34"), false);
  }
  const prepared = requests[1]!;
  assert.deepEqual(prepared.arguments, {
    workspace_path: "/tmp/workspace",
    proposal_public_id: PROPOSAL_ID,
    operator_actor_id: "111",
    proposal_version: 2,
    content_hash: CONTENT_HASH,
  });
  assert.equal(prepared.idempotency_key, `bridge-prepare-receipt:${PROPOSAL_ID}`);
  const applied = requests[2]!;
  assert.deepEqual(applied.arguments, {
    workspace_path: "/tmp/workspace",
    proposal_public_id: PROPOSAL_ID,
    operator_actor_id: "111",
    command_filename: "fact-set.json",
  });
  const authorization = requests[4]!;
  assert.equal(authorization.arguments.expected_calculation_snapshot_hash, SNAPSHOT_HASH);
  const finalized = requests[6]!;
  assert.deepEqual(finalized.arguments, {
    workspace_path: "/tmp/workspace",
    proposal_public_id: PROPOSAL_ID,
    operator_actor_id: "111",
    proposal_version: 2,
    content_hash: CONTENT_HASH,
    receipt_only: true,
  });
});

test("snapshot review refuses a missing, malformed, or non-string receipt identity", async () => {
  for (const malformedKind of ["missing", "non-string", "unsafe"] as const) {
    const command = createFinanceCommand(() => ({
      workspaceRoot: "/tmp/workspace",
      runner: {
        async run(request: BridgeRequest): Promise<BridgeResponse> {
          if (request.command !== "get_finalization_snapshot_review") {
            throw new Error("unexpected command");
          }
          const result: JsonObject = {
            identity_kind: "finalization_snapshot_review",
            proposal_public_id: PROPOSAL_ID,
            receipt_public_id: "rcpt_1",
            fact_set_public_id: "riaf_1",
            fact_set_version: 1,
            fact_set_result_hash: "d".repeat(64),
            calculation_snapshot_id: "snap_1",
            calculation_snapshot_hash: SNAPSHOT_HASH,
            currency: "SGD",
            payer_participant_public_id: "ptcp_owner",
            total_paid: "12.34",
            total_to_collect: "0.00",
            participant_shares: { ptcp_owner: "12.34" },
            settlement_obligations: [],
          };
          if (malformedKind === "missing") delete result.receipt_public_id;
          if (malformedKind === "non-string") result.receipt_public_id = 7;
          if (malformedKind === "unsafe") result.receipt_public_id = "unsafe receipt";
          return ok(request, result);
        },
      },
    }));

    const result = await command.handler(commandContext(`review-snapshot ${PROPOSAL_ID}`));
    assert.equal(
      result.text,
      `Receipt command outcome is unknown. Run /finance receipt-status ${PROPOSAL_ID}.`,
    );
  }
});

test("unverifiable receipt-operation responses are unknown and recover through status", async () => {
  const command = createFinanceCommand(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        if (request.command === "get_finalization_snapshot_review") {
          return ok(request, {
            identity_kind: "finalization_snapshot_review",
            proposal_public_id: PROPOSAL_ID,
            receipt_public_id: "rcpt_1",
            fact_set_public_id: "riaf_1",
            fact_set_version: 1,
            fact_set_result_hash: "d".repeat(64),
            calculation_snapshot_id: "snap_1",
            calculation_snapshot_hash: SNAPSHOT_HASH,
            currency: "SGD",
            payer_participant_public_id: "ptcp_owner",
            total_paid: 12.34,
            total_to_collect: "0.00",
            participant_shares: { ptcp_owner: "12.34" },
            settlement_obligations: [],
          });
        }
        throw new Error("unexpected command");
      },
    },
  }));

  const result = await command.handler(commandContext(`review-snapshot ${PROPOSAL_ID}`));
  assert.equal(result.continueAgent, false);
  assert.equal(
    result.text,
    `Receipt command outcome is unknown. Run /finance receipt-status ${PROPOSAL_ID}.`,
  );
});

test("verified retryable receipt refusals stay truthful and never claim an unknown outcome", async () => {
  const command = createFinanceCommand(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        if (request.command === "get_status") return ok(request, statusResult());
        if (request.command === "finalize") return failure(request, true);
        throw new Error("unexpected command");
      },
    },
  }));

  const result = await command.handler(commandContext(`finalize ${PROPOSAL_ID}`));
  assert.equal(result.continueAgent, false);
  assert.equal("submitText" in result, false);
  assert.match(result.text ?? "", /temporarily refused\. Run \/finance receipt-status .* and retry\./u);
  assert.doesNotMatch(result.text ?? "", /unknown/u);
});

test("receipt commands refuse malformed or unbound contexts before the runner", async () => {
  let runnerCalls = 0;
  const command = createFinanceCommand(() => ({
    workspaceRoot: "/tmp/workspace",
    runner: {
      async run(): Promise<BridgeResponse> {
        runnerCalls += 1;
        throw new Error("refused command must not reach the runner");
      },
    },
  }));
  const unbound = await command.handler(commandContext(`finalize ${PROPOSAL_ID}`, {
    async getCurrentConversationBinding() { return null; },
  }));
  const malformed = await command.handler(commandContext(`apply-facts ${PROPOSAL_ID} ../facts.json`));
  const group = await command.handler(commandContext(`finalize ${PROPOSAL_ID}`, {
    messageThreadId: 1,
  }));

  for (const result of [unbound, malformed, group]) {
    assert.equal(result.continueAgent, false);
  }
  assert.equal(runnerCalls, 0);
});
