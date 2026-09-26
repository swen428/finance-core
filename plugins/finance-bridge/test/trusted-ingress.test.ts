import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import type {
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
} from "openclaw-sdk/plugin-sdk/plugin-entry";

import type { BridgeRunner } from "../src/controller.js";
import type { HandoffPublisher } from "../src/handoff.js";
import type { ReceiptMediaAdapter, ValidatedMedia } from "../src/media.js";
import { canonicalCaptureKey, captureIdentities, type BridgeRequest, type BridgeResponse, type JsonObject } from "../src/protocol.js";
import { TrustedIngressCapture, type TrustedFinanceIngress } from "../src/trusted-ingress.js";

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00]);
const sha256 = (value: Buffer | string): string => createHash("sha256").update(value).digest("hex");
const digest = (value: JsonObject): string => sha256(JSON.stringify(Object.fromEntries(
  Object.entries(value).sort(([a], [b]) => a.localeCompare(b)),
)));

const binding = {
  bindingId: "binding-1", pluginId: "finance-bridge", pluginRoot: "/plugin",
  channel: "telegram", accountId: "finance-account", conversationId: "111",
  parentConversationId: "111", boundAt: 1_750_000_000, data: { senderId: "111" },
} as const;

function turn(params: { text?: string; image?: Buffer; unavailable?: boolean; messageId?: string } = {}) {
  const messageId = params.messageId ?? "20";
  const ingress: TrustedFinanceIngress = {
    channel: "telegram", accountId: "finance-account", updateId: 41,
    chatId: "111", messageId, senderId: "111", bindingId: "binding-1",
    payloadSha256: "a".repeat(64),
    ...(params.image === undefined ? {} : { attachmentSha256: sha256(params.image) }),
    ...(params.unavailable === true ? { attachmentUnavailable: true as const } : {}),
  };
  const event = {
    content: params.text ?? (params.image === undefined ? "lunch 12.50" : "receipt"),
    timestamp: 1_750_000_000_000, channel: "telegram", accountId: "finance-account",
    conversationId: "111", parentConversationId: "111", senderId: "111",
    messageId, isGroup: false, commandAuthorized: true, senderIsOwner: true,
    ...(params.image === undefined || params.unavailable === true ? {} : {
      metadata: { mediaUrl: "media://inbound/image", mediaType: "image/jpeg" },
    }),
    financeIngress: ingress,
  } as PluginHookInboundClaimEvent & { financeIngress: TrustedFinanceIngress };
  const context = {
    channelId: "telegram", accountId: "finance-account", conversationId: "111",
    senderId: "111", messageId, pluginBinding: binding,
  } as PluginHookInboundClaimContext;
  return { event, context, ingress };
}

function success(request: BridgeRequest, result: JsonObject): BridgeResponse {
  return {
    envelopeVersion: "v1", requestId: request.request_id,
    operationId: `op_${"a".repeat(32)}`, status: "ok", result, idempotentReplay: false,
  };
}

function missing(request: BridgeRequest): BridgeResponse {
  return {
    envelopeVersion: "v1", requestId: request.request_id,
    operationId: `op_${"a".repeat(32)}`, status: "error",
    error: { code: "INTAKE_NOT_FOUND", message: "missing", retryable: false },
  };
}

class FakeCore implements BridgeRunner {
  readonly calls: BridgeRequest[] = [];
  readonly stored = new Map<string, { kind: string; ingress: JsonObject; attachment: string | null }>();
  beforeCommit?: () => Promise<void>;
  loseResponse = false;
  available = true;
  originalAvailable = true;
  readonly intakeByMessage = new Map<string, string>();

  private jobId(intakeId: string): string {
    return `fcj_${sha256(`finance-capture-job-v1\0${intakeId}`).slice(0, 40)}`;
  }

  async run(request: BridgeRequest): Promise<BridgeResponse> {
    this.calls.push(request);
    if (!this.available) throw new Error("Core unavailable");
    if (request.command === "get_capture_job_for_message") {
      const messageId = String(request.arguments.telegram_message_id);
      const intakeId = this.intakeByMessage.get(messageId);
      return success(request, { candidate: intakeId === undefined ? null : {
        job_public_id: this.jobId(intakeId), telegram_message_id: messageId,
        source_identity_sha256: "c".repeat(64),
      } });
    }
    if (request.command === "get_status") {
      const jobId = request.arguments.job_public_id as string;
      const intakeId = [...this.stored.keys()].find((id) => this.jobId(id) === jobId);
      if (intakeId === undefined) return missing(request);
      const item = this.stored.get(intakeId);
      if (item === undefined) return missing(request);
      return success(request, {
        identity_kind: "intake", intake_public_id: intakeId, final_transaction_created: false,
        capture_attachment_integrity: item.attachment === null ? null :
          this.originalAvailable ? "verified" : "missing",
        capture_job: {
          public_id: jobId, intake_public_id: intakeId, capture_kind: item.kind,
          ingress_identity_digest: digest(item.ingress), attachment_content_hash: item.attachment,
        },
      });
    }
    assert.ok(request.command === "capture_interaction" || request.command === "capture");
    const ingress = request.arguments.finance_ingress as JsonObject;
    const key = canonicalCaptureKey(String(ingress.chatId), String(ingress.messageId));
    assert.equal(request.idempotency_key, key);
    if (request.command === "capture_interaction") {
      assert.equal((request.arguments.telegram_update as JsonObject).update_id, ingress.updateId);
    } else {
      assert.equal(request.arguments.telegram_update_id, ingress.updateId);
      assert.equal(request.arguments.handoff_content_hash, ingress.attachmentSha256);
      if (request.arguments.caption === "完成") {
        return {
          envelopeVersion: "v1", requestId: request.request_id,
          operationId: `op_${"a".repeat(32)}`, status: "error",
          error: {
            code: "ARGUMENTS_REFUSED",
            message: "Receipt caption resembles a control message; send the instruction as text.",
            retryable: false,
          },
        };
      }
    }
    await this.beforeCommit?.();
    const intakeId = this.intakeByMessage.get(String(ingress.messageId)) ??
      (request.command === "capture"
        ? captureIdentities(key).rawIntakePublicId
        : `raw_intake_12345678-1234-4234-8234-${Number(ingress.messageId).toString(16).padStart(12, "0")}`);
    const previous = this.stored.get(intakeId);
    if (previous !== undefined && digest(previous.ingress) !== digest(ingress)) {
      return {
        envelopeVersion: "v1", requestId: request.request_id,
        operationId: `op_${"a".repeat(32)}`, status: "error",
        error: { code: "IDEMPOTENCY_CONFLICT", message: "different source", retryable: false },
      };
    }
    this.stored.set(intakeId, {
      kind: request.command === "capture" ? "receipt_image" : "text",
      ingress, attachment: request.command === "capture" ? ingress.attachmentSha256 as string : null,
    });
    this.intakeByMessage.set(String(ingress.messageId), intakeId);
    if (this.loseResponse) throw new Error("response lost after commit");
    return success(request, { intake_public_id: intakeId, capture_job: { public_id: this.jobId(intakeId) } });
  }
}

function capture(core: FakeCore, options: {
  image?: Buffer; unavailable?: boolean; meter?: { acquire: number; publish: number };
} = {}) {
  const bytes = options.image ?? jpeg;
  const media: ValidatedMedia = {
    bytes, byteSize: bytes.length, contentHash: sha256(bytes),
    detectedMimeType: bytes === png ? "image/png" : "image/jpeg",
    canonicalExtension: bytes === png ? ".png" : ".jpg",
  };
  const mediaAdapter = {
    acquire: async () => {
      if (options.meter) options.meter.acquire += 1;
      if (options.unavailable) {
        throw new Error("missing media");
      }
      return media;
    },
  } as unknown as ReceiptMediaAdapter;
  const handoff = {
    withPublished: async (_key: string, _intake: string, _media: ValidatedMedia,
      callback: (published: { handoffFilename: string }, fd: number) => Promise<unknown>) => {
      if (options.meter) options.meter.publish += 1;
      return await callback({ handoffFilename: "original.jpg" }, 3);
    },
  } as unknown as HandoffPublisher;
  return new TrustedIngressCapture("/synthetic", core, mediaAdapter, handoff);
}

test("host receives adoption only after Core commits; D2 control text never invokes inline AI/OCR", async () => {
  const core = new FakeCore();
  let release!: () => void;
  core.beforeCommit = () => new Promise<void>((resolve) => { release = resolve; });
  const { event, context } = turn({ text: "完成" });
  const pending = capture(core).handle(event, context);
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(core.stored.size, 0);
  assert.deepEqual(core.calls.map((call) => call.command), ["get_capture_job_for_message", "capture_interaction"]);
  release();
  const result = await pending;
  assert.equal(result.handled, true);
  assert.equal(result.adoption?.schema, "finance-ingress-adoption-v1");
  assert.deepEqual(core.calls.map((call) => call.command), [
    "get_capture_job_for_message", "capture_interaction", "get_status",
  ]);
});

test("duplicate text replay and restart return same durable job", async () => {
  const core = new FakeCore();
  const { event, context } = turn();
  const first = await capture(core).handle(event, context);
  const replay = await capture(core).handle(event, context);
  assert.equal(replay.adoption?.jobId, first.adoption?.jobId);
  assert.equal(replay.adoption?.intakeId, first.adoption?.intakeId);
  assert.equal(core.stored.size, 1);
});

for (const image of [jpeg, png]) {
  test(`photo ${image === jpeg ? "JPEG" : "PNG"} proves original before adoption`, async () => {
    const core = new FakeCore();
    const { event, context, ingress } = turn({ image, text: "receipt" });
    const result = await capture(core, { image }).handle(event, context);
    assert.equal(result.adoption?.attachmentStatus, "stored");
    assert.equal(result.adoption?.attachmentSha256, ingress.attachmentSha256);
    assert.deepEqual(core.calls.map((call) => call.command), [
      "get_capture_job_for_message", "capture", "get_status",
    ]);
  });
}

test("control caption photo is refused by Core without adoption or inline processing", async () => {
  const core = new FakeCore();
  const { event, context } = turn({ image: jpeg, text: "完成" });
  const result = await capture(core, { image: jpeg }).handle(event, context);
  assert.deepEqual(result, { handled: false });
  assert.equal(core.stored.size, 0);
  assert.deepEqual(core.calls.map((call) => call.command), [
    "get_capture_job_for_message", "capture", "get_capture_job_for_message",
  ]);
});

for (const [name, value] of [
  ["mediaStagingPending", false], ["mediaType", "image/jpeg"],
  ["mediaTypes", []], ["originalFilename", "receipt.jpg"],
  ["mediaUrl", "media://inbound/x"], ["mediaUrls", []],
  ["mediaPath", "/tmp/x"], ["mediaPaths", []],
] as const) {
  test(`text without attachment digest refuses metadata ${name}`, async () => {
    const core = new FakeCore();
    const { event, context } = turn();
    event.metadata = { [name]: value };
    assert.deepEqual(await capture(core).handle(event, context), { handled: false });
    assert.deepEqual(core.calls, []);
  });
}

test("text without attachment digest refuses malformed metadata", async () => {
  const core = new FakeCore();
  const { event, context } = turn();
  event.metadata = [] as unknown as Record<string, unknown>;
  assert.deepEqual(await capture(core).handle(event, context), { handled: false });
  assert.deepEqual(core.calls, []);
});

test("missing photo without prior Core custody returns typed reupload refusal", async () => {
  const core = new FakeCore();
  const { event, context } = turn({ image: jpeg, unavailable: true });
  const result = await capture(core).handle(event, context);
  assert.equal(result.handled, false);
  assert.equal(result.financeIngressRefusal?.kind, "reupload_required");
  assert.equal(result.adoption, undefined);
  assert.deepEqual(core.calls.map((call) => call.command), ["get_capture_job_for_message"]);
});

test("missing replay photo can adopt only an existing verified original", async () => {
  const core = new FakeCore();
  const meter = { acquire: 0, publish: 0 };
  const first = turn({ image: jpeg });
  const bridge = capture(core, { image: jpeg, meter });
  await bridge.handle(first.event, first.context);
  const replay = turn({ image: jpeg, unavailable: true });
  const result = await bridge.handle(replay.event, replay.context);
  assert.equal(result.adoption?.attachmentStatus, "stored");
  assert.equal(result.adoption?.attachmentUnavailable, true);
  assert.equal(result.financeIngressRefusal, undefined);
  assert.deepEqual(meter, { acquire: 1, publish: 1 });
  assert.equal(core.calls.filter((call) => call.command === "capture").length, 1);
});

test("concurrent duplicate photo waits for one capture and never downloads or writes twice", async () => {
  const core = new FakeCore();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  core.beforeCommit = () => gate;
  const meter = { acquire: 0, publish: 0 };
  const bridge = capture(core, { image: jpeg, meter });
  const { event, context } = turn({ image: jpeg });
  const first = bridge.handle(event, context);
  const second = bridge.handle(event, context);
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.deepEqual(meter, { acquire: 1, publish: 1 });
  release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.adoption?.jobId, b.adoption?.jobId);
  assert.equal(core.calls.filter((call) => call.command === "capture").length, 1);
  assert.deepEqual(meter, { acquire: 1, publish: 1 });
});

test("persisted photo with lost original returns reupload refusal", async () => {
  const core = new FakeCore();
  const first = turn({ image: jpeg });
  await capture(core, { image: jpeg }).handle(first.event, first.context);
  core.originalAvailable = false;
  const replay = turn({ image: jpeg, unavailable: true });
  const result = await capture(core).handle(replay.event, replay.context);
  assert.equal(result.handled, false);
  assert.equal(result.financeIngressRefusal?.kind, "reupload_required");
  assert.equal(result.adoption, undefined);
});

test("Core outage and forged digest never produce adoption or reupload refusal", async () => {
  const core = new FakeCore();
  core.available = false;
  const missingPhoto = turn({ image: jpeg, unavailable: true });
  const outage = await capture(core).handle(missingPhoto.event, missingPhoto.context);
  assert.deepEqual(outage, { handled: false });
  core.available = true;
  const ordinary = turn();
  await capture(core).handle(ordinary.event, ordinary.context);
  ordinary.event.financeIngress.payloadSha256 = "b".repeat(64);
  const mismatch = await capture(core).handle(ordinary.event, ordinary.context);
  assert.deepEqual(mismatch, { handled: false });
  ordinary.event.financeIngress.payloadSha256 = "a".repeat(64);
  ordinary.event.financeIngress.updateId += 1;
  const updateMismatch = await capture(core).handle(ordinary.event, ordinary.context);
  assert.deepEqual(updateMismatch, { handled: false });
  assert.equal(core.calls.filter((call) => call.command === "capture_interaction").length, 1);
});

test("lost Core response is recovered by read only status after durable commit", async () => {
  const core = new FakeCore();
  core.loseResponse = true;
  const { event, context } = turn({ image: jpeg });
  const result = await capture(core, { image: jpeg }).handle(event, context);
  assert.equal(result.adoption?.attachmentStatus, "stored");
  assert.deepEqual(core.calls.map((call) => call.command), [
    "get_capture_job_for_message", "capture", "get_capture_job_for_message", "get_status",
  ]);
});

test("queue pressure refuses the ninth in flight update before capture", async () => {
  const core = new FakeCore();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  core.beforeCommit = () => gate;
  const ingress = capture(core);
  const pending = Array.from({ length: 8 }, (_, index) => {
    const { event, context } = turn({ messageId: String(index + 20) });
    return ingress.handle(event, context);
  });
  await new Promise<void>((resolve) => setImmediate(resolve));
  const ninth = turn({ messageId: "99" });
  assert.deepEqual(await ingress.handle(ninth.event, ninth.context), { handled: false });
  assert.equal(core.calls.length, 16);
  release();
  const results = await Promise.all(pending);
  assert.ok(results.every((result) => result.adoption !== undefined));
});
