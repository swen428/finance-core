import { createHash, randomBytes } from "node:crypto";
export const ENVELOPE_VERSION = "v1";
export const MAX_REQUEST_BYTES = 262_144;
export const MAX_RESPONSE_BYTES = 1_048_576;
export const MAX_ENVELOPE_DEPTH = 8;
export const MAX_IDEMPOTENCY_KEY_LENGTH = 200;
export const MODEL_EVALUATION_VERIFICATION_REASONS_V1 = [
    "ACCEPTANCE_POLICY_INVALID",
    "AMBIGUITY_FLAGS_EXTRA",
    "AMBIGUITY_FLAGS_MISMATCH",
    "AMBIGUITY_FLAGS_MISSING",
    "CASE_IDENTITY_INVALID",
    "CONFIDENCE_SHAPE_INVALID",
    "CONFIDENCE_VALUE_INVALID",
    "EVIDENCE_REFERENCE_INVALID",
    "EVIDENCE_REFS_SHAPE_INVALID",
    "FIELD_MISMATCH",
    "HARNESS_CONTRACT_INVALID",
    "HARNESS_OUTCOME_FIELDS_INVALID",
    "NULL_FIELD_HAS_EVIDENCE",
    "PRESENT_FIELD_LACKS_EVIDENCE",
    "PROMPT_INJECTION_ECHO",
    "RESPONSE_ADMISSION_INVALID",
    "RESPONSE_DUPLICATE_KEY",
    "RESPONSE_ENCODING_INVALID",
    "RESPONSE_JSON_INVALID",
    "RESPONSE_NOT_OBJECT",
    "RESPONSE_OVERSIZED",
    "RESPONSE_SCHEMA_INVALID",
    "RESPONSE_UTF8_INVALID",
    "VERIFIER_INPUT_INVALID",
];
const MODEL_EVALUATION_VERIFICATION_REASON_SET = new Set(MODEL_EVALUATION_VERIFICATION_REASONS_V1);
const MODEL_EVALUATION_VERIFICATION_FIELDS = new Set([
    "account", "amount", "category", "currency", "description", "intent_type", "merchant",
    "transaction_date",
]);
export function safeModelEvaluationRefusalDetailsV1(error) {
    if (error.code !== "AI_MODEL_EVAL_REFUSED")
        return undefined;
    const reason = error.details?.verification_reason;
    if (typeof reason !== "string" ||
        !MODEL_EVALUATION_VERIFICATION_REASON_SET.has(reason))
        return undefined;
    const field = error.details?.verification_field;
    return {
        verification_reason: reason,
        ...(typeof field === "string" && MODEL_EVALUATION_VERIFICATION_FIELDS.has(field)
            ? { verification_field: field }
            : {}),
    };
}
const RESPONSE_SUCCESS_FIELDS = new Set([
    "envelope_version",
    "request_id",
    "operation_id",
    "status",
    "result",
    "idempotent_replay",
]);
const RESPONSE_ERROR_FIELDS = new Set([
    "envelope_version",
    "request_id",
    "operation_id",
    "status",
    "error",
]);
const OPERATION_ID = /^op_[0-9a-f]{32}$/u;
function asciiJsonString(value) {
    return JSON.stringify(value).replace(/[\u007f-\u{10ffff}]/gu, (character) => {
        const codePoint = character.codePointAt(0);
        if (codePoint <= 0xffff)
            return `\\u${codePoint.toString(16).padStart(4, "0")}`;
        const offset = codePoint - 0x10000;
        const high = 0xd800 + (offset >> 10);
        const low = 0xdc00 + (offset & 0x3ff);
        return `\\u${high.toString(16)}\\u${low.toString(16)}`;
    });
}
function compareUnicode(left, right) {
    const leftPoints = [...left].map((character) => character.codePointAt(0));
    const rightPoints = [...right].map((character) => character.codePointAt(0));
    for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
        if (leftPoints[index] !== rightPoints[index])
            return leftPoints[index] - rightPoints[index];
    }
    return leftPoints.length - rightPoints.length;
}
function canonicalJson(value) {
    if (value === null)
        return "null";
    if (typeof value === "string")
        return asciiJsonString(value);
    if (typeof value === "boolean" || typeof value === "number")
        return JSON.stringify(value);
    if (Array.isArray(value))
        return `[${value.map(canonicalJson).join(",")}]`;
    return `{${Object.keys(value).sort(compareUnicode).map((key) => (`${asciiJsonString(key)}:${canonicalJson(value[key])}`)).join(",")}}`;
}
export function expectedBridgeOperationId(request) {
    const canonicalArguments = canonicalJson(request.arguments);
    const digest = canonicalDigest("operation", request.command, request.idempotency_key ?? "", canonicalArguments);
    return `op_${digest.slice(0, 32)}`;
}
function requireObject(value, label) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new Error(`${label} must be an object.`);
    }
    return value;
}
function requireExactFields(object, expected) {
    const keys = Object.keys(object);
    if (keys.length !== expected.size || keys.some((key) => !expected.has(key))) {
        throw new Error("Bridge response contains unknown or missing fields.");
    }
}
function checkDepth(root) {
    const stack = [[root, 1]];
    while (stack.length > 0) {
        const [node, depth] = stack.pop();
        if (depth > MAX_ENVELOPE_DEPTH) {
            throw new Error("Bridge response exceeds the maximum nesting depth.");
        }
        if (Array.isArray(node)) {
            for (const child of node)
                stack.push([child, depth + 1]);
        }
        else if (typeof node === "object" && node !== null) {
            for (const child of Object.values(node))
                stack.push([child, depth + 1]);
        }
    }
}
function canonicalDigest(...parts) {
    return createHash("sha256").update(parts.join("\0"), "utf8").digest("hex");
}
export function framedDigest(domain, ...fields) {
    if (!/^[\x21-\x7e]{1,100}$/u.test(domain))
        throw new Error("Digest domain is invalid.");
    const count = Buffer.allocUnsafe(4);
    count.writeUInt32BE(fields.length);
    const hash = createHash("sha256").update(domain, "ascii").update(Buffer.from([0])).update(count);
    for (const field of fields) {
        const encoded = Buffer.from(field, "utf8");
        const length = Buffer.allocUnsafe(4);
        length.writeUInt32BE(encoded.length);
        hash.update(length).update(encoded);
    }
    return hash.digest("hex");
}
export function createHumanActionBatchId() {
    return randomBytes(16).toString("hex");
}
export function humanActionIssuanceKey(batchId) {
    if (!/^(?:[0-9a-f]{32}|[0-9a-f]{64})$/u.test(batchId)) {
        throw new Error("Human action batch ID is invalid.");
    }
    return `bridge-human-action-issue:${batchId}`;
}
export function humanActionRedemptionKey(callbackId) {
    if (callbackId.length === 0 || callbackId.length > 200) {
        throw new Error("Telegram callback ID is invalid.");
    }
    return `bridge-human-action-redeem:${canonicalDigest(callbackId).slice(0, 32)}`;
}
export function postingReviewPreparationKey(cardGenerationPublicId) {
    if (!/^d1card_[0-9a-f]{32}$/u.test(cardGenerationPublicId)) {
        throw new Error("D2 card generation ID is invalid.");
    }
    return `bridge-d2-prepare:${cardGenerationPublicId}`;
}
export function initialPostingReviewPreparationKey(proposalPublicId, admittedSourceMessageId) {
    if (!/^(?:prop_bridge_[0-9a-f]{32}|parser_output_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u
        .test(proposalPublicId) || !Number.isSafeInteger(admittedSourceMessageId) ||
        admittedSourceMessageId <= 0) {
        throw new Error("D2 initial posting review identity is invalid.");
    }
    return `bridge-d2-prepare-initial:${proposalPublicId}:${admittedSourceMessageId}`;
}
export function postingActionIssuanceKey(reviewPublicId) {
    if (!/^d2rev_[0-9a-f]{30}$/u.test(reviewPublicId)) {
        throw new Error("D2 posting review ID is invalid.");
    }
    return `bridge-d2-issue:${reviewPublicId}`;
}
export function postingConfirmationKey(callbackId) {
    if (callbackId.length === 0 || callbackId.length > 200) {
        throw new Error("Telegram callback ID is invalid.");
    }
    return `bridge-d2-confirm:${canonicalDigest(callbackId).slice(0, 32)}`;
}
export function postingResumeKey(attemptPublicId) {
    if (!/^d2att_[0-9a-f]{30}$/u.test(attemptPublicId)) {
        throw new Error("D2 posting attempt ID is invalid.");
    }
    return `bridge-d2-resume:${attemptPublicId}`;
}
export function humanDraftOperationId(accountId, conversationId, bindingId, messageId, cardGenerationPublicId) {
    if (!Number.isSafeInteger(messageId) || messageId <= 0 ||
        !/^d1card_[0-9a-f]{32}$/u.test(cardGenerationPublicId)) {
        throw new Error("Human draft operation identity is invalid.");
    }
    return `d1op_${framedDigest("d1-plugin-card-operation-v1", accountId, conversationId, bindingId, String(messageId), cardGenerationPublicId).slice(0, 32)}`;
}
export function humanDraftApplyKey(operationPublicId) {
    if (!/^d1op_[0-9a-f]{32}$/u.test(operationPublicId)) {
        throw new Error("Human draft operation identity is invalid.");
    }
    return `bridge-human-draft-apply:${operationPublicId}`;
}
export function humanDraftDeliveryAttemptId(cardGenerationPublicId, transportMode) {
    if (!/^d1card_[0-9a-f]{32}$/u.test(cardGenerationPublicId)) {
        throw new Error("Human draft card identity is invalid.");
    }
    return framedDigest("d1-card-delivery-v1", cardGenerationPublicId, transportMode);
}
export function humanDraftDeliveryKey(attemptPublicId) {
    if (!/^[0-9a-f]{64}$/u.test(attemptPublicId))
        throw new Error("Delivery identity is invalid.");
    return `bridge-human-draft-delivery:${attemptPublicId}`;
}
export function humanDraftObservationId(attemptPublicId, slot) {
    if (!/^[0-9a-f]{64}$/u.test(attemptPublicId))
        throw new Error("Delivery identity is invalid.");
    return framedDigest("d1-card-observation-v1", attemptPublicId, slot);
}
export function humanDraftObservationKey(observationPublicId) {
    if (!/^[0-9a-f]{64}$/u.test(observationPublicId)) {
        throw new Error("Delivery observation identity is invalid.");
    }
    return `bridge-human-draft-observation:${observationPublicId}`;
}
export function humanDraftRecoveryId(draftPublicId, originalOperationOrStartPublicId, cardGenerationPublicId) {
    if (!/^d1draft_[0-9a-f]{32}$/u.test(draftPublicId) ||
        originalOperationOrStartPublicId.length === 0 ||
        originalOperationOrStartPublicId.length > 200 ||
        !/^d1card_[0-9a-f]{32}$/u.test(cardGenerationPublicId)) {
        throw new Error("Human draft recovery identity is invalid.");
    }
    return framedDigest("d1-card-recovery-v1", draftPublicId, originalOperationOrStartPublicId, cardGenerationPublicId);
}
export function humanDraftRecoveryKey(recoveryPublicId) {
    if (!/^[0-9a-f]{64}$/u.test(recoveryPublicId))
        throw new Error("Recovery identity is invalid.");
    return `bridge-human-draft-reissue:${recoveryPublicId}`;
}
export function guidedEditUpdateKey(sessionPublicId, messageId) {
    if (!/^gedit_[0-9a-f]{32}$/u.test(sessionPublicId) ||
        !Number.isSafeInteger(messageId) || messageId <= 0) {
        throw new Error("Guided edit update identity is invalid.");
    }
    return `bridge-guided-edit-update:${sessionPublicId}:${messageId}`;
}
export function guidedEditCompleteKey(sessionPublicId, messageId) {
    if (!/^gedit_[0-9a-f]{32}$/u.test(sessionPublicId) ||
        !Number.isSafeInteger(messageId) || messageId <= 0) {
        throw new Error("Guided edit completion identity is invalid.");
    }
    return `bridge-guided-edit-complete:${sessionPublicId}:${messageId}`;
}
export function canonicalCaptureKey(chatId, messageId) {
    if (!/^-?[0-9]+$/u.test(chatId) || !/^[0-9]+$/u.test(messageId)) {
        throw new Error("Telegram chat_id and message_id must be canonical decimal integers.");
    }
    return `raw-intake:telegram:${chatId}:${messageId}`;
}
export function captureIdentities(idempotencyKey) {
    const digest = canonicalDigest("openclaw-bridge-capture-v1", idempotencyKey).slice(0, 32);
    return {
        rawIntakePublicId: `raw_intake_bridge_${digest}`,
        attachmentEvidencePublicId: `tgae_bridge_${digest}`,
        extractionPublicId: `rocr_bridge_${digest}`,
        proposalPublicId: `prop_bridge_${digest}`,
        linkPublicId: `ropl_bridge_${digest}`,
    };
}
export function createBridgeRequest(command, arguments_, idempotencyKey) {
    if (idempotencyKey !== undefined &&
        (idempotencyKey.length === 0 || idempotencyKey.length > MAX_IDEMPOTENCY_KEY_LENGTH ||
            !/^[\x20-\x7e]+$/u.test(idempotencyKey))) {
        throw new Error("idempotency_key is not a bounded safe ASCII string.");
    }
    return {
        envelope_version: ENVELOPE_VERSION,
        command,
        request_id: `req_${randomBytes(16).toString("hex")}`,
        ...(idempotencyKey === undefined ? {} : { idempotency_key: idempotencyKey }),
        arguments: arguments_,
    };
}
export function serializeBridgeRequest(request) {
    const serialized = Buffer.from(JSON.stringify(request), "utf8");
    if (serialized.byteLength > MAX_REQUEST_BYTES) {
        throw new Error("Bridge request exceeds the 256 KiB stdin limit.");
    }
    return serialized;
}
export function parseBridgeResponse(raw, request) {
    if (raw.byteLength > MAX_RESPONSE_BYTES) {
        throw new Error("Bridge response exceeds the 1 MiB stdout limit.");
    }
    let text;
    try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
    }
    catch (error) {
        throw new Error("Bridge stdout is not valid UTF-8.", { cause: error });
    }
    const lines = text.split(/\r?\n/u).filter((line) => line.length > 0);
    if (lines.length !== 1) {
        throw new Error("Bridge stdout must contain exactly one JSON line.");
    }
    let parsed;
    try {
        parsed = JSON.parse(lines[0]);
    }
    catch (error) {
        throw new Error("Bridge stdout is not valid JSON.", { cause: error });
    }
    checkDepth(parsed);
    const object = requireObject(parsed, "Bridge response");
    if (object.envelope_version !== ENVELOPE_VERSION) {
        throw new Error("Bridge response envelope_version mismatch.");
    }
    if (object.request_id !== request.request_id && !(object.status === "error" && object.request_id === null)) {
        throw new Error("Bridge response request_id mismatch.");
    }
    if (object.operation_id !== null &&
        (typeof object.operation_id !== "string" || !OPERATION_ID.test(object.operation_id))) {
        throw new Error("Bridge response operation_id is invalid.");
    }
    if (object.status === "ok") {
        requireExactFields(object, RESPONSE_SUCCESS_FIELDS);
        if (object.operation_id === null) {
            throw new Error("Successful bridge response requires operation_id.");
        }
        if (typeof object.idempotent_replay !== "boolean") {
            throw new Error("Bridge response idempotent_replay is invalid.");
        }
        const result = requireObject(object.result, "Bridge result");
        return {
            envelopeVersion: ENVELOPE_VERSION,
            requestId: request.request_id,
            operationId: object.operation_id,
            status: "ok",
            result,
            idempotentReplay: object.idempotent_replay,
        };
    }
    if (object.status !== "error") {
        throw new Error("Bridge response status is invalid.");
    }
    requireExactFields(object, RESPONSE_ERROR_FIELDS);
    const error = requireObject(object.error, "Bridge error");
    const errorFields = new Set(Object.keys(error));
    const expected = new Set(["code", "message", "retryable"]);
    if (errorFields.has("details"))
        expected.add("details");
    if (errorFields.size !== expected.size || [...errorFields].some((key) => !expected.has(key))) {
        throw new Error("Bridge error contains unknown or missing fields.");
    }
    if (typeof error.code !== "string" || typeof error.message !== "string" ||
        typeof error.retryable !== "boolean") {
        throw new Error("Bridge error fields are invalid.");
    }
    const details = error.details === undefined
        ? undefined
        : requireObject(error.details, "Bridge error details");
    return {
        envelopeVersion: ENVELOPE_VERSION,
        requestId: typeof object.request_id === "string" ? object.request_id : null,
        operationId: object.operation_id,
        status: "error",
        error: {
            code: error.code,
            message: error.message,
            retryable: error.retryable,
            ...(details === undefined ? {} : { details }),
        },
    };
}
