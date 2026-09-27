import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import type {
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
} from "openclaw-sdk/plugin-sdk/plugin-entry";

import type { BridgeRunner } from "../src/controller.js";
import type { HandoffPublisher } from "../src/handoff.js";
import { FinanceIngressReconciliationV1 } from "../src/ingress-reconciliation-v1.js";
import { captureIdentities, canonicalCaptureKey, type BridgeRequest, type BridgeResponse, type JsonObject } from "../src/protocol.js";
import { photoIntakeFingerprint, type TrustedFinanceIngress } from "../src/trusted-ingress.js";

const hash = (value: string): string => createHash("sha256").update(value).digest("hex");
const nonce = "nonce-0123456789abcdef";
const binding = {
  bindingId: "binding-1", pluginId: "finance-bridge", pluginRoot: "/plugin",
  channel: "telegram", accountId: "finance-account", conversationId: "111",
  parentConversationId: "111", boundAt: 1_750_000_000, data: { senderId: "111" },
};

function original(text = "lunch 12.50", photo = false) {
  const ingress: TrustedFinanceIngress = {
    channel: "telegram", accountId: "finance-account", updateId: 41,
    chatId: "111", messageId: "20", senderId: "111", bindingId: "binding-1",
    payloadSha256: "a".repeat(64),
    ...(photo ? { attachmentSha256: "b".repeat(64) } : {}),
  };
  const event = {
    content: text, timestamp: 1_750_000_000_000, channel: "telegram",
    accountId: "finance-account", conversationId: "111", parentConversationId: "111",
    senderId: "111", messageId: "20", isGroup: false,
    commandAuthorized: true, senderIsOwner: true,
    ...(photo ? { metadata: { mediaUrl: "media://original", mediaType: "image/jpeg" } } : {}),
    financeIngress: ingress,
  } as PluginHookInboundClaimEvent & { financeIngress: TrustedFinanceIngress };
  const context = {
    channelId: "telegram", accountId: "finance-account", conversationId: "111",
    senderId: "111", messageId: "20", pluginBinding: binding,
  } as PluginHookInboundClaimContext;
  return { event, context, nonce };
}

function success(request: BridgeRequest, result: JsonObject): BridgeResponse {
  return {
    envelopeVersion: "v1", requestId: request.request_id,
    operationId: `op_${"a".repeat(32)}`, status: "ok", result, idempotentReplay: false,
  };
}

class Core implements BridgeRunner {
  readonly calls: BridgeRequest[] = [];
  noJob = false;
  partial = false;
  missingImage = false;
  routeMissing = false;
  financialState = "unposted";
  nextAction = "capture_processing_required";
  replyOutbox: JsonObject[] = [];
  resultMissing = false;
  sourceText = "lunch 12.50";
  photo = false;
  statusIngress?: TrustedFinanceIngress;

  readonly intakeId = "raw_intake_12345678-1234-4234-8234-000000000020";

  get jobId(): string {
    const intake = this.photo ? captureIdentities(canonicalCaptureKey("111", "20")).rawIntakePublicId : this.intakeId;
    return `fcj_${hash(`finance-capture-job-v1\0${intake}`).slice(0, 40)}`;
  }

  get actualIntakeId(): string {
    return this.photo ? captureIdentities(canonicalCaptureKey("111", "20")).rawIntakePublicId : this.intakeId;
  }

  async run(request: BridgeRequest): Promise<BridgeResponse> {
    this.calls.push(request);
    const ingress = this.statusIngress ?? original(this.sourceText, this.photo).event.financeIngress;
    const identity = {
      accountId: ingress.accountId, attachmentSha256: ingress.attachmentSha256,
      bindingId: ingress.bindingId, channel: ingress.channel, chatId: Number(ingress.chatId),
      messageId: Number(ingress.messageId), payloadSha256: ingress.payloadSha256,
      senderId: Number(ingress.senderId), updateId: ingress.updateId,
    };
    const ingressDigest = hash(JSON.stringify(Object.fromEntries(
      Object.entries(identity).filter(([, value]) => value !== undefined).sort(([a], [b]) => a.localeCompare(b)),
    )));
    const job = {
      public_id: this.jobId, intake_public_id: this.actualIntakeId,
      status: "awaiting_user", capture_kind: this.photo ? "receipt_image" : "text",
      ingress_identity_digest: ingressDigest,
      attachment_content_hash: this.photo ? ingress.attachmentSha256! : null,
      intake_fingerprint: this.photo
        ? photoIntakeFingerprint(111, 20, this.sourceText, ingress.attachmentSha256!) : "unused",
    };
    if (request.command === "get_capture_job_for_message") {
      const sourceIdentity = JSON.stringify({
        authenticated_actor_id: "111", conversation_binding_id: "binding-1",
        source_message_id: "20", telegram_account_id: "finance-account",
        telegram_conversation_id: "111", version: "finance_d2_telegram_source_context_v1",
      });
      return success(request, { candidate: this.noJob ? null : {
        job_public_id: this.jobId, telegram_message_id: "20",
        source_identity_sha256: hash(sourceIdentity),
      } });
    }
    if (request.command === "get_status") return success(request, {
      identity_kind: "intake", intake_public_id: this.actualIntakeId,
      final_transaction_created: false, capture_job: this.partial ? null : job,
      capture_attachment_integrity: this.photo ? this.missingImage ? "missing" : "verified" : null,
    });
    if (request.command === "get_interaction_route") return success(request, {
      found: !this.routeMissing,
      interaction_route: this.routeMissing ? null : {
        job_public_id: this.jobId, route_kind: "initial_intake", raw_text_sha256: hash(this.sourceText),
        authenticated_actor_id: "111", telegram_account_id: "finance-account",
        telegram_conversation_id: "111", conversation_binding_id: "binding-1",
        telegram_message_id: 20,
      },
      capture_job: job,
    });
    if (request.command === "get_capture_recovery") return success(request, {
      job_public_id: this.jobId, capture_status: "awaiting_user",
      financial_state: this.financialState, next_action: this.nextAction,
      reply_outbox: this.replyOutbox,
      result: !this.resultMissing && ["finalized", "corrected"].includes(this.financialState)
        ? { result_kind: "posting", result_public_id: "transaction-1" } : null,
    });
    throw new Error(`Unexpected command: ${request.command}`);
  }
}

function reconciler(core: Core, residue = false): FinanceIngressReconciliationV1 {
  const handoff = {
    async pendingPublicationKeyHash() { return residue ? "c".repeat(64) : undefined; },
    async pendingReclaims() { return []; },
  } as unknown as Pick<HandoffPublisher, "pendingPublicationKeyHash" | "pendingReclaims">;
  return new FinanceIngressReconciliationV1("/workspace", core, handoff);
}

test("a saved text replay returns one verified locator and current posted state without writes", async () => {
  const core = new Core();
  core.financialState = "finalized";
  core.replyOutbox = [{ status: "sent" }];
  const reconcile = reconciler(core);
  const first = await reconcile.reconcile(original());
  const second = await reconcile.reconcile(original());
  assert.deepEqual(first, second);
  assert.equal(first.kind, "matched");
  if (first.kind === "matched") {
    assert.equal(first.adoption.jobId, core.jobId);
    assert.equal(first.financialState, "finalized");
    assert.equal(first.replyState, "sent");
  }
  assert.deepEqual(core.calls.map((call) => call.command), [
    "get_capture_job_for_message", "get_status", "get_interaction_route", "get_capture_recovery",
    "get_capture_job_for_message", "get_status", "get_interaction_route", "get_capture_recovery",
  ]);
});

test("no job and partial intake are refused without capture", async () => {
  const core = new Core();
  core.noJob = true;
  assert.deepEqual(await reconciler(core).reconcile(original()), {
    schema: "finance-ingress-reconciliation-v1", kind: "refused", nonce, reason: "no_job",
  });
  assert.deepEqual(core.calls.map((call) => call.command), ["get_capture_job_for_message"]);
  core.noJob = false;
  core.partial = true;
  assert.equal((await reconciler(core).reconcile(original())).kind, "refused");
  assert.deepEqual(core.calls.slice(1).map((call) => call.command), [
    "get_capture_job_for_message", "get_status",
  ]);
});

test("changed text, missing route, and changed binding refuse old text", async () => {
  const core = new Core();
  assert.equal((await reconciler(core).reconcile(original(" lunch 12.50"))).kind, "refused");
  core.routeMissing = true;
  assert.equal((await reconciler(core).reconcile(original())).kind, "refused");
  const changed = original();
  changed.event.financeIngress = { ...changed.event.financeIngress, bindingId: "other" };
  assert.equal((await reconciler(core).reconcile(changed)).kind, "refused");
});

test("photo replay verifies exact caption fingerprint and stored original", async () => {
  const core = new Core();
  core.photo = true;
  core.sourceText = "receipt";
  assert.equal((await reconciler(core).reconcile(original("receipt", true))).kind, "matched");
  assert.equal((await reconciler(core).reconcile(original("receipt ", true))).kind, "refused");
  assert.deepEqual(await reconciler(core).reconcile(original("   ", true)), {
    schema: "finance-ingress-reconciliation-v1", kind: "refused", nonce,
    reason: "invalid_original",
  });
  core.missingImage = true;
  assert.equal((await reconciler(core).reconcile(original("receipt", true))).kind, "refused");
});

test("corrected and unknown financial outcomes come only from recovery GET", async () => {
  const core = new Core();
  core.financialState = "corrected";
  core.nextAction = "enqueue_existing_result";
  const corrected = await reconciler(core).reconcile(original());
  assert.equal(corrected.kind === "matched" && corrected.financialState, "corrected");
  assert.equal(corrected.kind === "matched" && corrected.replyState, "missing");
  core.replyOutbox = [{ status: "outcome_unknown" }];
  const mixed = await reconciler(core).reconcile(original());
  assert.equal(mixed.kind === "matched" && mixed.replyState, "outcome_unknown");
  core.replyOutbox = [];
  core.financialState = "unposted";
  core.nextAction = "ai_outcome_unknown";
  const unknown = await reconciler(core).reconcile(original());
  assert.equal(unknown.kind === "matched" && unknown.financialState, "unposted");
  assert.equal(unknown.kind === "matched" && unknown.replyState, "none");
});

test("unknown financial or reply state refuses instead of returning a locator", async () => {
  const core = new Core();
  core.financialState = "unexpected";
  assert.deepEqual(await reconciler(core).reconcile(original()), {
    schema: "finance-ingress-reconciliation-v1", kind: "refused", nonce,
    reason: "evidence_conflict",
  });
  core.financialState = "finalized";
  core.replyOutbox = [{ status: "retrying" }];
  assert.equal((await reconciler(core).reconcile(original())).kind, "refused");
  core.replyOutbox = [];
  core.resultMissing = true;
  assert.equal((await reconciler(core).reconcile(original())).kind, "refused");
  core.resultMissing = false;
  core.nextAction = "unreviewed_new_action";
  assert.equal((await reconciler(core).reconcile(original())).kind, "refused");
});

test("handoff residue prevents reconciliation", async () => {
  const core = new Core();
  assert.deepEqual(await reconciler(core, true).reconcile(original()), {
    schema: "finance-ingress-reconciliation-v1", kind: "refused", nonce, reason: "handoff_residue",
  });
  assert.equal(core.calls.length, 0);
});
