import { createHash } from "node:crypto";

import type {
  PluginHookInboundClaimContext,
  PluginHookInboundClaimEvent,
  PluginHookInboundClaimResult,
} from "openclaw-sdk/plugin-sdk/plugin-entry";

import type { BridgeRunner } from "./controller.js";
import type { HandoffPublisher, ReclaimClaim } from "./handoff.js";
import type { ReceiptMediaAdapter, ValidatedMedia } from "./media.js";
import { canonicalCaptureKey, captureIdentities, createBridgeRequest, type JsonObject } from "./protocol.js";

const SHA256 = /^[0-9a-f]{64}$/u;
const INTAKE_ID = /^(?:raw_intake_bridge_[0-9a-f]{32}|raw_intake_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u;
const JOB_ID = /^fcj_[0-9a-f]{40}$/u;
const COMMAND_DEADLINE_MS = 30_000;
const RECLAIM_PASS_DEADLINE_MS = 45_000;
const MAX_IN_FLIGHT_CAPTURES = 8;

/** These fields are copied from the pinned host contract; SDK packages may lag the pinned host. */
export interface TrustedFinanceIngress {
  channel: "telegram";
  accountId: string;
  updateId: number;
  chatId: string;
  messageId: string;
  senderId: string;
  bindingId: string;
  payloadSha256: string;
  attachmentSha256?: string;
  attachmentUnavailable?: true;
}

export interface FinanceIngressAdoption extends TrustedFinanceIngress {
  schema: "finance-ingress-adoption-v1";
  intakeId: string;
  jobId: string;
  attachmentStatus: "none" | "stored";
}

export interface FinanceIngressRefusal extends TrustedFinanceIngress {
  schema: "finance-ingress-refusal-v1";
  kind: "reupload_required";
}

export type TrustedClaimResult = PluginHookInboundClaimResult & {
  adoption?: FinanceIngressAdoption;
  financeIngressRefusal?: FinanceIngressRefusal;
};

interface ValidatedTurn {
  ingress: TrustedFinanceIngress;
  chatId: number;
  messageId: number;
  senderId: number;
  date: number;
  text: string;
  photo: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveDecimal(value: string): number | undefined {
  if (!/^[1-9][0-9]*$/u.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

function ingressFromEvent(event: PluginHookInboundClaimEvent): unknown {
  return (event as PluginHookInboundClaimEvent & { financeIngress?: unknown }).financeIngress;
}

export function hasTrustedFinanceIngress(event: PluginHookInboundClaimEvent): boolean {
  return ingressFromEvent(event) !== undefined;
}

function validateTurn(
  event: PluginHookInboundClaimEvent,
  context: PluginHookInboundClaimContext,
): ValidatedTurn | undefined {
  const candidate = ingressFromEvent(event);
  if (!isRecord(candidate) || !isRecord(context.pluginBinding)) return undefined;
  const binding = context.pluginBinding;
  const required = ["channel", "accountId", "updateId", "chatId", "messageId", "senderId", "bindingId", "payloadSha256"];
  if (required.some((key) => candidate[key] === undefined) ||
      Object.keys(candidate).some((key) => ![...required, "attachmentSha256", "attachmentUnavailable"].includes(key)) ||
      candidate.channel !== "telegram" || candidate.accountId !== event.accountId ||
      candidate.accountId !== context.accountId || candidate.accountId !== binding.accountId ||
      candidate.chatId !== event.conversationId || candidate.chatId !== context.conversationId ||
      candidate.chatId !== binding.conversationId ||
      candidate.messageId !== event.messageId || candidate.messageId !== context.messageId ||
      candidate.senderId !== event.senderId || candidate.senderId !== context.senderId ||
      candidate.senderId !== (isRecord(binding.data) ? binding.data.senderId : undefined) ||
      candidate.bindingId !== binding.bindingId || binding.pluginId !== "finance-bridge" ||
      binding.channel !== "telegram" || context.channelId !== "telegram" ||
      event.channel !== "telegram" || event.isGroup !== false ||
      event.threadId !== undefined || binding.threadId !== undefined ||
      (event.parentConversationId !== undefined && event.parentConversationId !== event.conversationId) ||
      (context.parentConversationId !== undefined && context.parentConversationId !== context.conversationId) ||
      (binding.parentConversationId !== undefined && binding.parentConversationId !== binding.conversationId) ||
      event.senderIsOwner !== true || event.commandAuthorized !== true ||
      typeof candidate.payloadSha256 !== "string" || !SHA256.test(candidate.payloadSha256) ||
      typeof candidate.accountId !== "string" || candidate.accountId.length === 0 ||
      typeof candidate.bindingId !== "string" || candidate.bindingId.length === 0 ||
      !Number.isSafeInteger(candidate.updateId) || (candidate.updateId as number) < 0 ||
      typeof candidate.chatId !== "string" || typeof candidate.messageId !== "string" ||
      typeof candidate.senderId !== "string" || typeof event.content !== "string" ||
      !Number.isSafeInteger(event.timestamp) || (event.timestamp as number) % 1_000 !== 0 ||
      (event.timestamp as number) < 1_262_304_000_000 ||
      (event.timestamp as number) > 253_402_300_799_000) return undefined;
  const chatId = positiveDecimal(candidate.chatId);
  const messageId = positiveDecimal(candidate.messageId);
  const senderId = positiveDecimal(candidate.senderId);
  if (chatId === undefined || messageId === undefined || senderId !== chatId) return undefined;
  const photo = candidate.attachmentSha256 !== undefined;
  if (photo && (typeof candidate.attachmentSha256 !== "string" || !SHA256.test(candidate.attachmentSha256))) return undefined;
  if (!photo && (candidate.attachmentUnavailable !== undefined || event.content.trim().length === 0)) return undefined;
  if (candidate.attachmentUnavailable !== undefined && candidate.attachmentUnavailable !== true) return undefined;
  if (!photo && event.metadata !== undefined &&
      (!isRecord(event.metadata) || [
        "mediaStagingPending", "mediaUrl", "mediaUrls", "mediaPath", "mediaPaths",
        "mediaType", "mediaTypes", "originalFilename",
      ].some((key) => event.metadata?.[key] !== undefined))) return undefined;
  return {
    ingress: candidate as unknown as TrustedFinanceIngress,
    chatId,
    messageId,
    senderId,
    date: (event.timestamp as number) / 1_000,
    text: event.content,
    photo,
  };
}

function coreIdentity(ingress: TrustedFinanceIngress): JsonObject {
  return {
    channel: ingress.channel,
    accountId: ingress.accountId,
    updateId: ingress.updateId,
    chatId: Number(ingress.chatId),
    messageId: Number(ingress.messageId),
    senderId: Number(ingress.senderId),
    bindingId: ingress.bindingId,
    payloadSha256: ingress.payloadSha256,
    ...(ingress.attachmentSha256 === undefined ? {} : { attachmentSha256: ingress.attachmentSha256 }),
  };
}

function ingressDigest(identity: JsonObject): string {
  const canonical = JSON.stringify(Object.fromEntries(
    Object.entries(identity).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0),
  ));
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

function caption(text: string): string | undefined {
  if (text.length === 0) return undefined;
  if (text.trim().length === 0) throw new Error("Receipt caption is invalid.");
  let count = 0;
  for (const character of text) {
    const point = character.codePointAt(0)!;
    if (point >= 0xd800 && point <= 0xdfff) throw new Error("Receipt caption is invalid.");
    count += 1;
  }
  if (count > 2_000) throw new Error("Receipt caption is too long.");
  return text;
}

function checkedStatus(
  result: JsonObject,
  turn: ValidatedTurn,
  expectedJobId: string,
): FinanceIngressAdoption | undefined {
  const job = result.capture_job;
  const intakeId = result.intake_public_id;
  if (!isRecord(job) || result.identity_kind !== "intake" ||
      typeof intakeId !== "string" || !INTAKE_ID.test(intakeId) ||
      job.intake_public_id !== intakeId ||
      typeof job.public_id !== "string" || !JOB_ID.test(job.public_id) ||
      job.public_id !== expectedJobId ||
      job.public_id !== `fcj_${createHash("sha256").update(`finance-capture-job-v1\0${intakeId}`).digest("hex").slice(0, 40)}` ||
      job.ingress_identity_digest !== ingressDigest(coreIdentity(turn.ingress)) ||
      job.capture_kind !== (turn.photo ? "receipt_image" : "text") ||
      result.final_transaction_created !== false) return undefined;
  if (turn.photo && (job.attachment_content_hash !== turn.ingress.attachmentSha256 ||
      result.capture_attachment_integrity !== "verified")) return undefined;
  if (!turn.photo && (job.attachment_content_hash !== null ||
      result.capture_attachment_integrity !== null)) return undefined;
  return {
    ...turn.ingress,
    schema: "finance-ingress-adoption-v1",
    intakeId,
    jobId: job.public_id,
    attachmentStatus: turn.photo ? "stored" : "none",
  };
}

export class TrustedIngressCapture {
  private inFlight = 0;
  private readonly activeByMessage = new Map<string, Promise<void>>();

  constructor(
    private readonly workspaceRoot: string,
    private readonly runner: BridgeRunner,
    private readonly media: ReceiptMediaAdapter,
    private readonly handoff: HandoffPublisher,
  ) {}

  private async verifyCoreCustody(claim: ReclaimClaim, deadlineAt?: number): Promise<boolean> {
    const remaining = deadlineAt === undefined ? COMMAND_DEADLINE_MS :
      Math.min(COMMAND_DEADLINE_MS, Math.ceil(deadlineAt - performance.now()));
    if (remaining <= 0) throw new Error("Trusted handoff recovery deadline exceeded.");
    const response = await this.runner.run(createBridgeRequest("get_status", {
      workspace_path: this.workspaceRoot,
      job_public_id: claim.jobPublicId,
    }), remaining);
    if (deadlineAt !== undefined && performance.now() >= deadlineAt) {
      throw new Error("Trusted handoff recovery deadline exceeded.");
    }
    if (response.status !== "ok") return false;
    const result = response.result;
    const job = result.capture_job;
    return result.identity_kind === "intake" &&
      result.intake_public_id === claim.rawIntakePublicId &&
      result.capture_attachment_integrity === "verified" &&
      result.final_transaction_created === false &&
      isRecord(job) && job.public_id === claim.jobPublicId &&
      job.intake_public_id === claim.rawIntakePublicId &&
      job.capture_kind === "receipt_image" &&
      job.ingress_identity_digest === claim.ingressIdentityDigest &&
      job.attachment_content_hash === claim.attachmentContentHash;
  }

  /** Called at plugin readiness, including when the Host has already ACKed. */
  async resumePendingReclaims(maxDurationMs = RECLAIM_PASS_DEADLINE_MS): Promise<void> {
    if (!Number.isSafeInteger(maxDurationMs) || maxDurationMs <= 0 ||
        maxDurationMs > RECLAIM_PASS_DEADLINE_MS) {
      throw new Error("Trusted handoff recovery budget is invalid.");
    }
    const deadline = performance.now() + maxDurationMs;
    for (const claim of await this.handoff.pendingReclaims()) {
      if (performance.now() >= deadline) {
        throw new Error("Trusted handoff recovery deadline exceeded.");
      }
      await this.handoff.reclaimVerified(claim,
        async (candidate) => await this.verifyCoreCustody(candidate, deadline), deadline);
      if (performance.now() >= deadline) {
        throw new Error("Trusted handoff recovery deadline exceeded.");
      }
    }
  }

  async handle(event: PluginHookInboundClaimEvent, context: PluginHookInboundClaimContext): Promise<TrustedClaimResult> {
    const turn = validateTurn(event, context);
    if (turn === undefined) return { handled: false };
    let receiptCaption: string | undefined;
    try {
      if (turn.photo) receiptCaption = caption(turn.text);
    } catch {
      return { handled: false };
    }
    const messageKey = `${turn.ingress.accountId}\0${turn.chatId}\0${turn.messageId}`;
    const existingOperation = this.activeByMessage.get(messageKey);
    if (existingOperation !== undefined) {
      await existingOperation;
      return await this.handle(event, context);
    }
    if (this.inFlight >= MAX_IN_FLIGHT_CAPTURES) return { handled: false };
    let release!: () => void;
    this.activeByMessage.set(messageKey, new Promise<void>((resolve) => { release = resolve; }));
    this.inFlight += 1;
    try {
      const key = canonicalCaptureKey(String(turn.chatId), String(turn.messageId));
      const photoClaim = (): ReclaimClaim => {
        const intakeId = captureIdentities(key).rawIntakePublicId;
        return {
          rawIntakePublicId: intakeId,
          jobPublicId: `fcj_${createHash("sha256").update(`finance-capture-job-v1\0${intakeId}`).digest("hex").slice(0, 40)}`,
          canonicalKeyHash: createHash("sha256").update(key).digest("hex"),
          ingressIdentityDigest: ingressDigest(coreIdentity(turn.ingress)),
          attachmentContentHash: turn.ingress.attachmentSha256!,
        };
      };
      const status = async (jobId: string): Promise<{
        adoption?: FinanceIngressAdoption; reupload?: boolean;
      }> => {
        const response = await this.runner.run(createBridgeRequest("get_status", {
          workspace_path: this.workspaceRoot,
          job_public_id: jobId,
        }), COMMAND_DEADLINE_MS);
        if (response.status !== "ok") return {};
        const adoption = checkedStatus(response.result, turn, jobId);
        if (adoption !== undefined) {
          if (!turn.photo) {
            const routeResponse = await this.runner.run(createBridgeRequest("get_interaction_route", {
              workspace_path: this.workspaceRoot,
              operator_actor_id: String(turn.senderId),
              telegram_account_id: turn.ingress.accountId,
              telegram_conversation_id: turn.ingress.chatId,
              conversation_binding_id: turn.ingress.bindingId,
              telegram_message_id: turn.messageId,
            }), COMMAND_DEADLINE_MS);
            if (routeResponse.status !== "ok" || routeResponse.result.found !== true ||
                routeResponse.result.final_transaction_created !== false) return {};
            const route = routeResponse.result.interaction_route;
            const routeJob = routeResponse.result.capture_job;
            const statusJob = response.result.capture_job;
            if (!isRecord(route) || !isRecord(routeJob) || !isRecord(statusJob) ||
                route.job_public_id !== adoption.jobId ||
                route.raw_text_sha256 !== createHash("sha256").update(turn.text, "utf8").digest("hex") ||
                route.authenticated_actor_id !== String(turn.senderId) ||
                route.telegram_account_id !== turn.ingress.accountId ||
                route.telegram_conversation_id !== turn.ingress.chatId ||
                route.conversation_binding_id !== turn.ingress.bindingId ||
                route.telegram_message_id !== turn.messageId ||
                !["initial_intake", "whole_card", "guided_update", "guided_complete", "control_refused"].includes(String(route.route_kind)) ||
                routeJob.public_id !== adoption.jobId ||
                routeJob.intake_public_id !== adoption.intakeId ||
                routeJob.capture_kind !== "text" ||
                routeJob.ingress_identity_digest !== statusJob.ingress_identity_digest ||
                routeJob.attachment_content_hash !== null) return {};
          }
          return { adoption };
        }
        if (turn.photo && response.result.capture_attachment_integrity === "missing" &&
            checkedStatus({ ...response.result, capture_attachment_integrity: "verified" }, turn, jobId)) {
          return { reupload: true };
        }
        return {};
      };
      const discover = async (): Promise<{ found: boolean; jobId?: string }> => {
        const response = await this.runner.run(createBridgeRequest("get_capture_job_for_message", {
          workspace_path: this.workspaceRoot,
          operator_actor_id: String(turn.senderId),
          telegram_account_id: turn.ingress.accountId,
          telegram_conversation_id: turn.ingress.chatId,
          conversation_binding_id: turn.ingress.bindingId,
          telegram_message_id: turn.messageId,
        }), COMMAND_DEADLINE_MS);
        if (response.status !== "ok") throw new Error("Capture discovery failed.");
        const candidate = response.result.candidate;
        if (candidate === null) return { found: false };
        if (!isRecord(candidate) || candidate.telegram_message_id !== turn.ingress.messageId ||
            typeof candidate.job_public_id !== "string" || !JOB_ID.test(candidate.job_public_id) ||
            typeof candidate.source_identity_sha256 !== "string" ||
            !SHA256.test(candidate.source_identity_sha256)) {
          throw new Error("Capture discovery identity is invalid.");
        }
        return { found: true, jobId: candidate.job_public_id };
      };
      // A prior turn may have ACKed immediately before a cleanup crash. Each
      // restart rechecks Core custody before resuming any durable intent.
      await this.resumePendingReclaims();
      const adoptPhoto = async (adoption: FinanceIngressAdoption): Promise<TrustedClaimResult> => {
        const claim = photoClaim();
        if (adoption.jobId !== claim.jobPublicId || adoption.intakeId !== claim.rawIntakePublicId) {
          return { handled: false };
        }
        if (!await this.handoff.isReclaimed(claim)) {
          await this.handoff.prepareRetainedReclaim(key, claim);
          if (!await this.handoff.reclaimVerified(claim,
            async (candidate) => await this.verifyCoreCustody(candidate))) return { handled: false };
        }
        return { handled: true, adoption };
      };
      const found = await discover();
      if (found.found && found.jobId !== undefined) {
        const existing = await status(found.jobId);
        if (existing.adoption !== undefined) return turn.photo
          ? await adoptPhoto(existing.adoption)
          : { handled: true, adoption: existing.adoption };
        if (turn.ingress.attachmentUnavailable === true && existing.reupload === true) {
          return { handled: false, financeIngressRefusal: {
            ...turn.ingress, schema: "finance-ingress-refusal-v1", kind: "reupload_required",
          } };
        }
        return { handled: false };
      }
      if (turn.ingress.attachmentUnavailable === true) {
        return {
          handled: false,
          financeIngressRefusal: {
            ...turn.ingress,
            schema: "finance-ingress-refusal-v1",
            kind: "reupload_required",
          },
        };
      }
      let capturedJobId: string | undefined;
      if (turn.photo) {
        if (event.metadata === undefined) return { handled: false };
        let media: ValidatedMedia;
        try {
          media = await this.media.acquireTrustedInbound(event.metadata, COMMAND_DEADLINE_MS);
        } catch {
          return { handled: false };
        }
        if (media.contentHash !== turn.ingress.attachmentSha256) return { handled: false };
        const intakeId = captureIdentities(key).rawIntakePublicId;
        try {
          capturedJobId = await this.handoff.withPublished(key, intakeId, media, async (published, payloadFd) => {
            const request = createBridgeRequest("capture", {
              workspace_path: this.workspaceRoot,
              kind: "receipt_image",
              handoff_filename: published.handoffFilename,
              handoff_descriptor_fd: 3,
              handoff_content_hash: media.contentHash,
              telegram_message_id: turn.messageId,
              telegram_chat_id: turn.chatId,
              telegram_update_id: turn.ingress.updateId,
              sender_id: turn.senderId,
              authenticated_actor_id: String(turn.senderId),
              telegram_account_id: turn.ingress.accountId,
              telegram_conversation_id: turn.ingress.chatId,
              conversation_binding_id: turn.ingress.bindingId,
              declared_mime_type: media.detectedMimeType,
              ...(media.originalFilename === undefined ? {} : { original_filename: media.originalFilename }),
              ...(receiptCaption === undefined ? {} : { caption: receiptCaption }),
              finance_ingress: coreIdentity(turn.ingress),
            }, key);
            let response;
            try { response = await this.runner.run(request, COMMAND_DEADLINE_MS, payloadFd); }
            catch { return undefined; }
            if (response.status !== "ok") return undefined;
            const job = response.result.capture_job;
            if (!isRecord(job) || typeof job.public_id !== "string" || !JOB_ID.test(job.public_id)) {
              return undefined;
            }
            return job.public_id;
          }, COMMAND_DEADLINE_MS, photoClaim());
        } catch {
          // A lost response may follow a durable Core commit. Only a matching read can adopt it.
          const recoveredId = await discover();
          if (recoveredId.jobId === undefined) return { handled: false };
          const recovered = await status(recoveredId.jobId);
          return recovered.adoption === undefined
            ? { handled: false }
            : await adoptPhoto(recovered.adoption);
        }
        if (capturedJobId === undefined) {
          const recoveredId = await discover();
          if (recoveredId.jobId === undefined) return { handled: false };
          const recovered = await status(recoveredId.jobId);
          return recovered.adoption === undefined
            ? { handled: false }
            : await adoptPhoto(recovered.adoption);
        }
      } else {
        try {
          const response = await this.runner.run(createBridgeRequest("capture_interaction", {
            workspace_path: this.workspaceRoot,
            telegram_update: {
              update_id: turn.ingress.updateId,
              message: {
                message_id: turn.messageId,
                chat: { id: turn.chatId, type: "private" },
                date: turn.date,
                from: { id: turn.senderId },
                text: turn.text,
              },
            },
            authenticated_actor_id: String(turn.senderId),
            telegram_account_id: turn.ingress.accountId,
            telegram_conversation_id: turn.ingress.chatId,
            conversation_binding_id: turn.ingress.bindingId,
            finance_ingress: coreIdentity(turn.ingress),
          }, key), COMMAND_DEADLINE_MS);
          if (response.status !== "ok") throw new Error("Core text capture refused.");
          const job = response.result.capture_job;
          if (!isRecord(job) || typeof job.public_id !== "string" || !JOB_ID.test(job.public_id)) {
            throw new Error("Core capture job ID is invalid.");
          }
          capturedJobId = job.public_id;
        } catch {
          const recoveredId = await discover();
          if (recoveredId.jobId === undefined) return { handled: false };
          const recovered = await status(recoveredId.jobId);
          return recovered.adoption === undefined
            ? { handled: false }
            : { handled: true, adoption: recovered.adoption };
        }
      }
      if (capturedJobId === undefined) return { handled: false };
      const adopted = await status(capturedJobId);
      return adopted.adoption === undefined
        ? { handled: false }
        : turn.photo ? await adoptPhoto(adopted.adoption)
          : { handled: true, adoption: adopted.adoption };
    } catch {
      // A subprocess can die after Core commits; status is queried on replay.
      return { handled: false };
    } finally {
      this.inFlight -= 1;
      this.activeByMessage.delete(messageKey);
      release();
    }
  }
}
