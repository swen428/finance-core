import type { JsonObject, JsonValue } from "./protocol.js";

const SAFE_TEXT = /^[^\p{C}\p{Zl}\p{Zp}]{1,256}$/u;
const PROCESSING_PATHS = new Set([
  "no_model",
  "local_model",
  "cloud_projection",
  "model_denied",
  "model_failed",
  "outcome_unknown",
]);
const SAFE_REASONS: Record<string, string> = {
  configuration_not_accepted: "模型配置未获接纳",
  policy_denied: "安全策略拒绝",
  deadline_exceeded: "处理超时",
  attribution_mismatch: "模型归属不匹配",
  provider_unavailable: "模型服务不可用",
  output_invalid: "模型输出无效",
  outcome_unknown: "处理结果未知",
  status_unavailable: "状态暂不可用",
};

export interface ProcessingStatusV2 {
  intakePublicId: string;
  attemptPublicId: string | null;
  receiptPublicId: string | null;
  admissionDecisionPublicId: string | null;
  processingPath: string;
  safeReasonCode: string | null;
  displayAlias: string | null;
  canonicalAttribution: {
    provider: string;
    model: string;
    agentId: "finance";
  } | null;
  attributionMatch: boolean | null;
}

function nullableString(value: JsonValue | undefined, pattern: RegExp): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error("AI processing status identity is invalid.");
  }
  return value;
}

function safeText(value: JsonValue | undefined): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !SAFE_TEXT.test(value) || value !== value.trim() ||
      Buffer.byteLength(value, "utf8") > 256) {
    throw new Error("AI processing status text is invalid.");
  }
  return value;
}

export function parseProcessingStatusV2(value: JsonObject): ProcessingStatusV2 {
  const expected = [
    "intake_public_id", "attempt_public_id", "receipt_public_id",
    "admission_decision_public_id", "processing_path", "safe_reason_code",
    "canonical_attribution", "display_alias", "attribution_match",
  ];
  if (Object.keys(value).sort().join(",") !== expected.sort().join(",")) {
    throw new Error("AI processing status fields are not exact.");
  }
  const intakePublicId = nullableString(
    value.intake_public_id,
    /^(?:raw_intake_bridge_[0-9a-f]{32}|raw_intake_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u,
  );
  if (intakePublicId === null) throw new Error("AI processing intake identity is missing.");
  const attemptPublicId = nullableString(value.attempt_public_id, /^aifa_[0-9a-f]{64}$/u);
  const receiptPublicId = nullableString(value.receipt_public_id, /^aimr_[0-9a-f]{64}$/u);
  const admissionDecisionPublicId = nullableString(
    value.admission_decision_public_id,
    /^aimd_[0-9a-f]{64}$/u,
  );
  const processingPath = safeText(value.processing_path);
  if (processingPath === null) throw new Error("AI processing path is missing.");
  const safeReasonCode = safeText(value.safe_reason_code);
  const displayAlias = safeText(value.display_alias);
  let canonicalAttribution: ProcessingStatusV2["canonicalAttribution"] = null;
  if (value.canonical_attribution !== null) {
    if (typeof value.canonical_attribution !== "object" ||
        Array.isArray(value.canonical_attribution)) {
      throw new Error("AI processing attribution is invalid.");
    }
    const attribution = value.canonical_attribution as JsonObject;
    if (Object.keys(attribution).sort().join(",") !== "agent_id,model,provider" ||
        attribution.agent_id !== "finance") {
      throw new Error("AI processing attribution is invalid.");
    }
    const provider = safeText(attribution.provider);
    const model = safeText(attribution.model);
    if (provider === null || model === null) {
      throw new Error("AI processing attribution is incomplete.");
    }
    canonicalAttribution = { provider, model, agentId: "finance" };
  }
  const attributionMatch = value.attribution_match;
  if (attributionMatch !== null && typeof attributionMatch !== "boolean") {
    throw new Error("AI processing attribution result is invalid.");
  }
  return {
    intakePublicId,
    attemptPublicId,
    receiptPublicId,
    admissionDecisionPublicId,
    processingPath,
    safeReasonCode,
    displayAlias,
    canonicalAttribution,
    attributionMatch,
  };
}

function refusalReason(status: ProcessingStatusV2): string {
  return status.safeReasonCode === null
    ? SAFE_REASONS.status_unavailable
    : SAFE_REASONS[status.safeReasonCode] ?? SAFE_REASONS.status_unavailable;
}

export function renderProcessingFooterV2(status: ProcessingStatusV2): string {
  if (!PROCESSING_PATHS.has(status.processingPath)) return `🛑 ${SAFE_REASONS.status_unavailable}`;
  if (status.processingPath === "no_model") return "🧮 未使用模型";
  if (status.processingPath === "local_model") {
    return status.displayAlias === null
      ? `🛑 ${SAFE_REASONS.status_unavailable}`
      : `💻 ${status.displayAlias}`;
  }
  if (status.processingPath === "cloud_projection") {
    return status.displayAlias === null
      ? `🛑 ${SAFE_REASONS.status_unavailable}`
      : `☁️ ${status.displayAlias}`;
  }
  return `🛑 ${refusalReason(status)}`;
}

export function renderProcessingStatusDetailV2(status: ProcessingStatusV2): string {
  const lines = [
    `Finance intake: ${status.intakePublicId}`,
    `Processing path: ${status.processingPath}`,
    `Display: ${renderProcessingFooterV2(status)}`,
  ];
  if (status.canonicalAttribution !== null) {
    lines.push(
      `Canonical attribution: ${status.canonicalAttribution.provider}/${status.canonicalAttribution.model}`,
      `Agent: ${status.canonicalAttribution.agentId}`,
    );
  }
  if (status.receiptPublicId !== null) lines.push(`Compatibility receipt: ${status.receiptPublicId}`);
  if (status.attemptPublicId !== null) lines.push(`Attempt: ${status.attemptPublicId}`);
  if (status.admissionDecisionPublicId !== null) {
    lines.push(`Admission decision: ${status.admissionDecisionPublicId}`);
  }
  return lines.join("\n");
}
