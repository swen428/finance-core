export declare const ENVELOPE_VERSION = "v1";
export declare const MAX_REQUEST_BYTES = 262144;
export declare const MAX_RESPONSE_BYTES = 1048576;
export declare const MAX_ENVELOPE_DEPTH = 8;
export declare const MAX_IDEMPOTENCY_KEY_LENGTH = 200;
export type JsonPrimitive = boolean | number | string | null;
export type JsonValue = JsonPrimitive | JsonValue[] | {
    [key: string]: JsonValue;
};
export type JsonObject = {
    [key: string]: JsonValue;
};
export interface BridgeRequest {
    envelope_version: typeof ENVELOPE_VERSION;
    command: string;
    request_id: string;
    arguments: JsonObject;
    idempotency_key?: string;
}
export interface BridgeSuccess {
    envelopeVersion: typeof ENVELOPE_VERSION;
    requestId: string;
    operationId: string;
    status: "ok";
    result: JsonObject;
    idempotentReplay: boolean;
}
export interface BridgeFailure {
    envelopeVersion: typeof ENVELOPE_VERSION;
    requestId: string | null;
    operationId: string | null;
    status: "error";
    error: {
        code: string;
        message: string;
        retryable: boolean;
        details?: JsonObject;
    };
}
export type BridgeResponse = BridgeSuccess | BridgeFailure;
export declare const MODEL_EVALUATION_VERIFICATION_REASONS_V1: readonly ["ACCEPTANCE_POLICY_INVALID", "AMBIGUITY_FLAGS_EXTRA", "AMBIGUITY_FLAGS_MISMATCH", "AMBIGUITY_FLAGS_MISSING", "CASE_IDENTITY_INVALID", "CONFIDENCE_SHAPE_INVALID", "CONFIDENCE_VALUE_INVALID", "EVIDENCE_REFERENCE_INVALID", "EVIDENCE_REFS_SHAPE_INVALID", "FIELD_MISMATCH", "HARNESS_CONTRACT_INVALID", "HARNESS_OUTCOME_FIELDS_INVALID", "NULL_FIELD_HAS_EVIDENCE", "PRESENT_FIELD_LACKS_EVIDENCE", "PROMPT_INJECTION_ECHO", "RESPONSE_ADMISSION_INVALID", "RESPONSE_DUPLICATE_KEY", "RESPONSE_ENCODING_INVALID", "RESPONSE_JSON_INVALID", "RESPONSE_NOT_OBJECT", "RESPONSE_OVERSIZED", "RESPONSE_SCHEMA_INVALID", "RESPONSE_UTF8_INVALID", "VERIFIER_INPUT_INVALID"];
export declare function safeModelEvaluationRefusalDetailsV1(error: BridgeFailure["error"]): JsonObject | undefined;
export declare function expectedBridgeOperationId(request: BridgeRequest): string;
export declare function framedDigest(domain: string, ...fields: string[]): string;
export declare function createHumanActionBatchId(): string;
export declare function humanActionIssuanceKey(batchId: string): string;
export declare function humanActionRedemptionKey(callbackId: string): string;
export declare function postingReviewPreparationKey(cardGenerationPublicId: string): string;
export declare function initialPostingReviewPreparationKey(proposalPublicId: string, admittedSourceMessageId: number): string;
export declare function postingActionIssuanceKey(reviewPublicId: string): string;
export declare function postingConfirmationKey(callbackId: string): string;
export declare function postingResumeKey(attemptPublicId: string): string;
export declare function humanDraftOperationId(accountId: string, conversationId: string, bindingId: string, messageId: number, cardGenerationPublicId: string): string;
export declare function humanDraftApplyKey(operationPublicId: string): string;
export declare function humanDraftDeliveryAttemptId(cardGenerationPublicId: string, transportMode: "replace" | "reply"): string;
export declare function humanDraftDeliveryKey(attemptPublicId: string): string;
export declare function humanDraftObservationId(attemptPublicId: string, slot: "initial" | "resolution"): string;
export declare function humanDraftObservationKey(observationPublicId: string): string;
export declare function humanDraftRecoveryId(draftPublicId: string, originalOperationOrStartPublicId: string, cardGenerationPublicId: string): string;
export declare function humanDraftRecoveryKey(recoveryPublicId: string): string;
export declare function guidedEditUpdateKey(sessionPublicId: string, messageId: number): string;
export declare function guidedEditCompleteKey(sessionPublicId: string, messageId: number): string;
export declare function canonicalCaptureKey(chatId: string, messageId: string): string;
export declare function captureIdentities(idempotencyKey: string): {
    rawIntakePublicId: string;
    attachmentEvidencePublicId: string;
    extractionPublicId: string;
    proposalPublicId: string;
    linkPublicId: string;
};
export declare function createBridgeRequest(command: string, arguments_: JsonObject, idempotencyKey?: string): BridgeRequest;
export declare function serializeBridgeRequest(request: BridgeRequest): Buffer;
export declare function parseBridgeResponse(raw: Buffer, request: BridgeRequest): BridgeResponse;
