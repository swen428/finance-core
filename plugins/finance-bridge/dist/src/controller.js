import { createHash } from "node:crypto";
import { extname } from "node:path";
import { canonicalCaptureKey, captureIdentities, createHumanActionBatchId, createBridgeRequest, guidedEditCompleteKey, guidedEditUpdateKey, humanActionIssuanceKey, } from "./protocol.js";
import { humanActionCallbackData, } from "./interactive.js";
import { parseProcessingStatusV2, renderProcessingFooterV2, } from "./processing-status-v2.js";
import { ReceiptMediaUnavailableError, } from "./media.js";
const COMMAND_DEADLINE_MS = 30_000;
const CONTROLLER_DEADLINE_MS = 105_000;
const MAX_TELEGRAM_TIMESTAMP_MS = 253_402_300_799_000;
const MAX_QUEUED_TURNS = 8;
const MAX_RECEIPT_CAPTION_CHARACTERS = 2_000;
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const RAW_INTAKE_ID = new RegExp(`^(?:raw_intake_bridge_[0-9a-f]{32}|raw_intake_${UUID})$`, "u");
const PROPOSAL_ID = new RegExp(`^(?:prop_bridge_[0-9a-f]{32}|parser_output_${UUID})$`, "u");
export const FINANCE_FAILURE_REPLY = "Finance intake could not be processed safely. Please retry.";
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
const GUIDED_EDIT_FIELD_ALIASES = new Map([
    ["amount", "amount"], ["金额", "amount"],
    ["currency", "currency"], ["币种", "currency"],
    ["transaction_date", "transaction_date"], ["date", "transaction_date"],
    ["日期", "transaction_date"],
    ["merchant", "merchant"], ["商户", "merchant"],
    ["description", "description"], ["描述", "description"],
    ["category", "category"], ["分类", "category"],
]);
function parseGuidedEditMessage(text) {
    const trimmed = text.trim();
    if (trimmed === "完成")
        return { kind: "complete" };
    const separator = trimmed.indexOf("=");
    if (separator <= 0 || separator !== trimmed.lastIndexOf("="))
        return { kind: "invalid" };
    const alias = trimmed.slice(0, separator).trim().toLowerCase();
    const value = trimmed.slice(separator + 1).trim();
    const field = GUIDED_EDIT_FIELD_ALIASES.get(alias);
    if (field === undefined || value.length === 0 || Buffer.byteLength(value, "utf8") > 1_024 ||
        /[\p{C}\p{Zl}\p{Zp}]/u.test(value))
        return { kind: "invalid" };
    return { kind: "update", field, value };
}
function looksLikeGuidedControl(text) {
    const trimmed = text.trim();
    const separator = trimmed.indexOf("=");
    return trimmed === "完成" || (separator > 0 && separator === trimmed.lastIndexOf("="));
}
const AI_FALLBACK_AGENT_ID = "finance";
const AI_FALLBACK_PURPOSE = "finance-bridge.ai-proposal-v2";
function sessionKeyHash(value) {
    if (value === undefined)
        return null;
    return createHash("sha256")
        .update("finance-ai-session-key-v1", "ascii")
        .update("\0", "ascii")
        .update(value, "utf8")
        .digest("hex");
}
function aiFallbackOutcome(result) {
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
function utf16Sha256(value) {
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
function metadataUtf16Sha256(field, value) {
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
function hasUnpairedSurrogate(value) {
    for (let index = 0; index < value.length; index += 1) {
        const codeUnit = value.charCodeAt(index);
        if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
            const next = value.charCodeAt(index + 1);
            if (next < 0xdc00 || next > 0xdfff)
                return true;
            index += 1;
        }
        else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
            return true;
        }
    }
    return false;
}
function metadataIssue(completion) {
    const fields = [
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
        if (value === undefined || value === null)
            continue;
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
    ]) {
        if (value !== undefined && value !== null &&
            (!Number.isSafeInteger(value) || value < 0 || value > 10_000_000)) {
            return { field, reason: "out_of_range", codeUnitCount: null, sha256: null };
        }
    }
    return undefined;
}
function requireFallbackModelCall(value, requestIdentity) {
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
function decimalInteger(value) {
    if (value === undefined || !/^[1-9][0-9]*$/u.test(value))
        return undefined;
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : undefined;
}
function bindingDataSender(value) {
    if (typeof value !== "object" || value === null || Array.isArray(value))
        return undefined;
    const sender = value.senderId;
    return typeof sender === "string" ? sender : undefined;
}
function isTopLevelParent(parent, conversation) {
    return parent === undefined || (conversation !== undefined && parent === conversation);
}
function validateTextTurn(event, context) {
    const content = typeof event.content === "string" ? event.content : undefined;
    const hasReceipt = isReceiptMetadata(event.metadata);
    const binding = context.pluginBinding;
    if (binding === undefined || binding.pluginId !== "finance-bridge")
        return undefined;
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
    const chatId = decimalInteger(event.conversationId);
    const senderId = decimalInteger(event.senderId);
    const messageId = decimalInteger(event.messageId);
    const timestampMs = event.timestamp;
    if (!Number.isSafeInteger(timestampMs) || timestampMs % 1_000 !== 0 ||
        timestampMs < 1_262_304_000_000 ||
        timestampMs > MAX_TELEGRAM_TIMESTAMP_MS ||
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
        date: timestampMs / 1_000,
        text: content,
    };
}
function requireOk(response) {
    if (response.status !== "ok") {
        throw new Error(`Bridge command refused with ${response.error.code}.`);
    }
    return response.result;
}
function requirePublicId(object, field, kind) {
    const value = object[field];
    const pattern = kind === "intake" ? RAW_INTAKE_ID : PROPOSAL_ID;
    if (typeof value !== "string" || !pattern.test(value)) {
        throw new Error(`Bridge result ${field} is invalid.`);
    }
    return value;
}
function renderReviewScalar(value, field) {
    if (value === null || value === undefined || value === "")
        return "not provided";
    if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 1_024 ||
        /[\p{C}\p{Zl}\p{Zp}]/u.test(value)) {
        throw new Error(`Review ${field} cannot be represented without changing it.`);
    }
    return value;
}
function renderFinancialScalar(value, field) {
    return renderOptionalReviewScalar(value, field);
}
function renderOptionalReviewScalar(value, field) {
    if (value === null || value === undefined)
        return undefined;
    if (typeof value === "string" && value.trim().length === 0) {
        throw new Error(`Review ${field} cannot be blank.`);
    }
    const rendered = renderReviewScalar(value, field);
    return rendered;
}
function renderBoundReviewLine(label, value, unset = "unset") {
    return `${label}: ${value === undefined ? unset : `set ${JSON.stringify(value)}`}`;
}
function requireReviewBinding(review) {
    const version = review.proposal_version;
    const contentHash = review.effective_content_hash;
    if (typeof version !== "number" || !Number.isSafeInteger(version) || version < 0 ||
        typeof contentHash !== "string" || !/^[0-9a-f]{64}$/u.test(contentHash)) {
        throw new Error("Review version binding is invalid.");
    }
    return { version, contentHash };
}
function renderAmbiguities(value) {
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
function receiptCaption(value) {
    if (value.trim().length === 0)
        return undefined;
    let characters = 0;
    for (const character of value) {
        const codePoint = character.codePointAt(0);
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
function renderReview(review, confirmAvailable) {
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
    if (source === undefined)
        throw new Error("Review source type is invalid.");
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
function requireActionReferences(result, confirmAvailable) {
    const actions = result.actions;
    if (!isJsonObject(actions))
        throw new Error("Human action references are missing.");
    const expected = confirmAvailable
        ? new Set(["confirm", "edit", "reject"])
        : new Set(["reject"]);
    if (Object.keys(actions).length !== expected.size ||
        Object.keys(actions).some((action) => !expected.has(action))) {
        throw new Error("Human action reference set is invalid.");
    }
    const output = {};
    for (const action of (confirmAvailable
        ? ["confirm", "edit", "reject"]
        : ["reject"])) {
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
function isJsonObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
export class FinanceInboundController {
    workspaceRoot;
    runner;
    receipt;
    controllerDeadlineMs;
    llmRuntime;
    queue = Promise.resolve();
    queuedTurns = 0;
    constructor(workspaceRoot, runner, receipt, controllerDeadlineMs = CONTROLLER_DEADLINE_MS, llmRuntime) {
        this.workspaceRoot = workspaceRoot;
        this.runner = runner;
        this.receipt = receipt;
        this.controllerDeadlineMs = controllerDeadlineMs;
        this.llmRuntime = llmRuntime;
    }
    async handle(event, context) {
        const turn = validateTextTurn(event, context);
        if (turn === undefined)
            return { handled: true };
        if (this.queuedTurns >= MAX_QUEUED_TURNS) {
            return { handled: true, reply: { text: FINANCE_FAILURE_REPLY } };
        }
        const admittedAt = performance.now();
        this.queuedTurns += 1;
        let release;
        const predecessor = this.queue;
        const completion = new Promise((resolve) => { release = resolve; });
        this.queue = predecessor.then(async () => await completion);
        let queueTimer;
        try {
            if (!Number.isSafeInteger(this.controllerDeadlineMs) || this.controllerDeadlineMs <= 0 ||
                this.controllerDeadlineMs > CONTROLLER_DEADLINE_MS) {
                throw new Error("Finance controller deadline is invalid.");
            }
            await Promise.race([
                predecessor,
                new Promise((_resolve, reject) => {
                    queueTimer = setTimeout(() => reject(new Error("Finance controller queue deadline exceeded.")), this.controllerDeadlineMs);
                }),
            ]);
            const remaining = Math.floor(this.controllerDeadlineMs - (performance.now() - admittedAt));
            if (remaining <= 0)
                throw new Error("Finance controller queue deadline exceeded.");
            const hasMedia = isReceiptMetadata(event.metadata);
            if (hasMedia)
                receiptCaption(turn.text);
            const guided = await this.runGuidedEditTurn(turn, hasMedia, remaining);
            if (guided !== undefined)
                return guided;
            return hasMedia
                ? await this.runReceiptTurn(turn, event.metadata, remaining)
                : await this.runTextTurn(turn, remaining);
        }
        catch {
            return { handled: true, reply: { text: FINANCE_FAILURE_REPLY } };
        }
        finally {
            if (queueTimer !== undefined)
                clearTimeout(queueTimer);
            this.queuedTurns -= 1;
            release();
        }
    }
    async runGuidedEditTurn(turn, hasMedia, admittedDeadlineMs) {
        const parsed = parseGuidedEditMessage(turn.text);
        const startedAt = performance.now();
        const deadline = () => {
            const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
            if (remaining <= 0)
                throw new Error("Finance controller deadline exceeded.");
            return Math.min(COMMAND_DEADLINE_MS, remaining);
        };
        const context = {
            workspace_path: this.workspaceRoot,
            operator_actor_id: String(turn.senderId),
            telegram_account_id: turn.accountId,
            telegram_conversation_id: String(turn.chatId),
            conversation_binding_id: turn.bindingId,
        };
        const lookup = requireOk(await this.runner.run(createBridgeRequest("get_guided_edit_session", { ...context, telegram_message_id: turn.messageId }), deadline()));
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
        const instructions = "Reply with one field=value message. Supported fields: 金额, 币种, 日期, 商户, 描述, 分类. " +
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
            const response = await this.runner.run(createBridgeRequest("complete_guided_edit", { ...context, session_public_id: sessionPublicId, telegram_message_id: turn.messageId }, guidedEditCompleteKey(sessionPublicId, turn.messageId)), deadline());
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
                response.result.final_transaction_created !== false) {
                throw new Error("Guided edit completion result is invalid.");
            }
            const proposalPublicId = requirePublicId(response.result, "proposal_public_id", "proposal");
            return await this.reviewProposal(proposalPublicId, turn, deadline, undefined, {
                sessionPublicId,
                messageId: turn.messageId,
                batchId: response.result.review_batch_id,
            });
        }
        const response = await this.runner.run(createBridgeRequest("apply_guided_edit_update", {
            ...context,
            session_public_id: sessionPublicId,
            telegram_message_id: turn.messageId,
            field_name: parsed.field,
            field_value: parsed.value,
        }, guidedEditUpdateKey(sessionPublicId, turn.messageId)), deadline());
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
    async runReceiptTurn(turn, metadata, admittedDeadlineMs) {
        if (this.receipt === undefined)
            throw new Error("Receipt intake is unavailable.");
        const startedAt = performance.now();
        const caption = receiptCaption(turn.text);
        const deadline = () => {
            const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
            if (remaining <= 0)
                throw new Error("Finance controller deadline exceeded.");
            return Math.min(COMMAND_DEADLINE_MS, remaining);
        };
        const key = canonicalCaptureKey(String(turn.chatId), String(turn.messageId));
        const identity = captureIdentities(key).rawIntakePublicId;
        const captureMedia = async (media, published, payloadFd) => requireOk(await this.runner.run(createBridgeRequest("capture", {
            workspace_path: this.workspaceRoot,
            kind: "receipt_image",
            handoff_filename: published.handoffFilename,
            handoff_descriptor_fd: 3,
            handoff_content_hash: media.contentHash,
            telegram_message_id: turn.messageId,
            telegram_chat_id: turn.chatId,
            telegram_message_date: turn.date,
            sender_id: turn.senderId,
            declared_mime_type: media.detectedMimeType,
            ...(media.originalFilename === undefined
                ? {}
                : { original_filename: media.originalFilename }),
            ...(caption === undefined ? {} : { caption }),
        }, key), deadline(), payloadFd));
        let capture;
        try {
            const media = await this.receipt.media.acquire(metadata, deadline());
            capture = await this.receipt.handoff.withPublished(key, identity, media, async (published, payloadFd) => await captureMedia(media, published, payloadFd), deadline());
        }
        catch (error) {
            if (!(error instanceof ReceiptMediaUnavailableError))
                throw error;
            const originalFilename = error.originalFilename;
            const declaredMimeType = error.declaredMimeType;
            const retainedCapture = await this.receipt.handoff.withRetained(key, identity, async (published, payloadFd, media) => {
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
                return await captureMedia(originalFilename === undefined ? media : { ...media, originalFilename }, published, payloadFd);
            }, deadline());
            if (retainedCapture === undefined)
                throw error;
            capture = retainedCapture;
        }
        return await this.proposeAndReviewCaptured(capture, turn, deadline);
    }
    async runTextTurn(turn, admittedDeadlineMs) {
        const startedAt = performance.now();
        const deadline = () => {
            const remaining = Math.floor(admittedDeadlineMs - (performance.now() - startedAt));
            if (remaining <= 0)
                throw new Error("Finance controller deadline exceeded.");
            return Math.min(COMMAND_DEADLINE_MS, remaining);
        };
        const key = canonicalCaptureKey(String(turn.chatId), String(turn.messageId));
        const capture = requireOk(await this.runner.run(createBridgeRequest("capture", {
            workspace_path: this.workspaceRoot,
            kind: "text",
            telegram_message: {
                message_id: turn.messageId,
                chat: { id: turn.chatId, type: "private" },
                date: turn.date,
                from: { id: turn.senderId },
                text: turn.text,
            },
        }, key), deadline()));
        return await this.proposeAndReviewCaptured(capture, turn, deadline);
    }
    async proposeAndReviewCaptured(capture, turn, deadline) {
        const intakePublicId = requirePublicId(capture, "intake_public_id", "intake");
        try {
            return await this.proposeAndReview(capture, turn, deadline);
        }
        catch {
            return {
                handled: true,
                reply: {
                    text: `${FINANCE_FAILURE_REPLY}\n\nFinance intake: ${intakePublicId}\n${await this.processingFooter(intakePublicId, deadline)}`,
                },
            };
        }
    }
    async proposeAndReview(capture, turn, deadline) {
        const intakePublicId = requirePublicId(capture, "intake_public_id", "intake");
        const proposed = requireOk(await this.runner.run(createBridgeRequest("propose", { workspace_path: this.workspaceRoot, intake_public_id: intakePublicId }, `bridge-propose:${intakePublicId}`), deadline()));
        const deterministicProposalPublicId = requirePublicId(proposed, "proposal_public_id", "proposal");
        let fallback;
        try {
            fallback = await this.runAiFallback(intakePublicId, deadline);
        }
        catch {
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
    async reviewProposal(proposalPublicId, turn, deadline, intakePublicId, guidedRecovery) {
        const review = requireOk(await this.runner.run(createBridgeRequest("get_review", { workspace_path: this.workspaceRoot, proposal_public_id: proposalPublicId }), deadline()));
        if (requirePublicId(review, "proposal_public_id", "proposal") !== proposalPublicId) {
            throw new Error("Review proposal identity does not match propose result.");
        }
        if (typeof review.confirm_available !== "boolean") {
            throw new Error("Review confirmation availability is invalid.");
        }
        const confirmAvailable = review.confirm_available;
        const reviewBinding = requireReviewBinding(review);
        const renderedReview = renderReview(review, confirmAvailable);
        const text = intakePublicId === undefined
            ? renderedReview
            : `${renderedReview}\n\nFinance intake: ${intakePublicId}\n${await this.processingFooter(intakePublicId, deadline)}`;
        let batchId = guidedRecovery?.batchId ?? createHumanActionBatchId();
        const issueBatch = async (currentBatchId) => {
            return await this.runner.run(createBridgeRequest("issue_human_actions", {
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
            }, humanActionIssuanceKey(currentBatchId)), deadline());
        };
        let issuedResponse = await issueBatch(batchId);
        if (guidedRecovery !== undefined && issuedResponse.status === "error" &&
            issuedResponse.error.code === "CALLBACK_EXPIRED") {
            const renewed = await this.runner.run(createBridgeRequest("complete_guided_edit", {
                workspace_path: this.workspaceRoot,
                operator_actor_id: String(turn.senderId),
                telegram_account_id: turn.accountId,
                telegram_conversation_id: String(turn.chatId),
                conversation_binding_id: turn.bindingId,
                session_public_id: guidedRecovery.sessionPublicId,
                telegram_message_id: guidedRecovery.messageId,
            }, guidedEditCompleteKey(guidedRecovery.sessionPublicId, guidedRecovery.messageId)), deadline());
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
        const references = requireActionReferences(issued, confirmAvailable);
        if (references.reject === undefined) {
            throw new Error("Reject action reference is missing.");
        }
        const buttons = [];
        if (confirmAvailable) {
            if (references.confirm === undefined) {
                throw new Error("Confirm action reference is missing.");
            }
            buttons.push({
                label: "Confirm",
                style: "success",
                action: { type: "callback", value: humanActionCallbackData("confirm", references.confirm) },
            });
            if (references.edit === undefined) {
                throw new Error("Edit action reference is missing.");
            }
            buttons.push({
                label: "Edit",
                action: { type: "callback", value: humanActionCallbackData("edit", references.edit) },
            });
        }
        buttons.push({
            label: "Reject",
            style: "danger",
            action: { type: "callback", value: humanActionCallbackData("reject", references.reject) },
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
    async processingFooter(intakePublicId, deadline) {
        try {
            const response = await this.runner.run(createBridgeRequest("get_ai_processing_status_v2", { workspace_path: this.workspaceRoot, intake_public_id: intakePublicId }), deadline());
            return renderProcessingFooterV2(parseProcessingStatusV2(requireOk(response)));
        }
        catch {
            return "🛑 状态暂不可用";
        }
    }
    async runAiFallback(intakePublicId, deadline) {
        if (this.llmRuntime === undefined)
            return { kind: "unavailable" };
        const recordStage = (stage, startedAt) => {
            const elapsedMs = Math.max(0, Math.min(120_000, Math.round(performance.now() - startedAt)));
            try {
                this.llmRuntime?.recordStageTiming?.(stage, elapsedMs);
            }
            catch {
                // Public-safe telemetry must never change Finance processing behavior.
            }
        };
        const projectionStartedAt = performance.now();
        let configProjection;
        try {
            configProjection = this.llmRuntime.currentProjection();
        }
        finally {
            recordStage("config_projection", projectionStartedAt);
        }
        const prepareStartedAt = performance.now();
        let preparedResponse;
        try {
            preparedResponse = await this.runner.run(createBridgeRequest("prepare_ai_fallback_v2", {
                workspace_path: this.workspaceRoot,
                intake_public_id: intakePublicId,
                config_projection: configProjection,
            }, aiFallbackKey("prepare_ai_fallback_v2", intakePublicId)), deadline());
        }
        finally {
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
        const claimedResponse = await this.runner.run(createBridgeRequest("claim_ai_fallback_invocation_v2", { workspace_path: this.workspaceRoot, attempt_public_id: attemptPublicId }, aiFallbackKey("claim_ai_fallback_invocation_v2", attemptPublicId)), deadline());
        if (claimedResponse.status !== "ok") {
            throw new Error(`AI fallback claim refused with ${claimedResponse.error.code}.`);
        }
        if (claimedResponse.result.invocation_disposition !== "invoke_once") {
            return { kind: "stopped", reason: "manual_recovery" };
        }
        const modelCall = requireFallbackModelCall(claimedResponse.result.model_call, prepared.request_identity);
        const callStartNotAfter = claimedResponse.result.call_start_not_after_ms;
        if (typeof callStartNotAfter !== "number" || !Number.isSafeInteger(callStartNotAfter)) {
            requireOk(await this.runner.run(createBridgeRequest("record_ai_fallback_result_v2", {
                workspace_path: this.workspaceRoot,
                attempt_public_id: attemptPublicId,
                transport_outcome: "local_preinvocation_refused",
                failure_code: "request_integrity_refused",
            }, aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId)), deadline()));
            return { kind: "stopped", reason: "manual_recovery" };
        }
        const resultNotAfterMs = prepared.result_not_after_ms;
        if (typeof resultNotAfterMs !== "number" || !Number.isSafeInteger(resultNotAfterMs)) {
            requireOk(await this.runner.run(createBridgeRequest("record_ai_fallback_result_v2", {
                workspace_path: this.workspaceRoot,
                attempt_public_id: attemptPublicId,
                transport_outcome: "local_preinvocation_refused",
                failure_code: "request_integrity_refused",
            }, aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId)), deadline()));
            return { kind: "stopped", reason: "manual_recovery" };
        }
        const terminal = new AbortController();
        let closed = false;
        let terminalSubmission;
        let terminalReason;
        let timeout;
        const submit = async (transportOutcome, details = {}) => {
            if (terminalSubmission !== undefined)
                return terminalSubmission;
            terminalReason = transportOutcome === "timeout"
                ? "timeout"
                : "settled";
            closed = true;
            terminal.abort(terminalReason);
            terminalSubmission = (async () => {
                const resultStartedAt = performance.now();
                try {
                    return requireOk(await this.runner.run(createBridgeRequest("record_ai_fallback_result_v2", {
                        workspace_path: this.workspaceRoot,
                        attempt_public_id: attemptPublicId,
                        transport_outcome: transportOutcome,
                        ...details,
                    }, aiFallbackKey("record_ai_fallback_result_v2", attemptPublicId)), deadline()));
                }
                finally {
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
        const invocationWindow = Math.min(outerRemainingMs, 30_000, resultGuardRemainingMs);
        if (invocationWindow <= 0) {
            await submit("timeout", { failure_code: "deadline_exceeded" });
            return { kind: "stopped", reason: "manual_recovery" };
        }
        const timeoutPromise = new Promise((_resolve, reject) => {
            timeout = setTimeout(() => {
                void submit("timeout", { failure_code: "deadline_exceeded" }).catch(() => undefined);
                reject(new Error("AI fallback timed out."));
            }, invocationWindow);
        });
        try {
            const completionStartedAt = performance.now();
            let completion;
            try {
                completion = await Promise.race([
                    this.llmRuntime.complete({
                        ...modelCall,
                        maxRetries: 0,
                        signal: terminal.signal,
                    }),
                    timeoutPromise,
                ]);
            }
            finally {
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
            let transportOutcome;
            let details = {
                returned_provider: completion.provider ?? null,
                returned_model: completion.model ?? null,
                returned_agent_id: completion.agentId ?? null,
                audit_caller_kind: completion.audit?.caller?.kind ?? null,
                audit_caller_id: completion.audit?.caller?.id ?? null,
                audit_caller_name: completion.audit?.caller?.name ?? null,
                audit_purpose: completion.audit?.purpose ?? null,
                audit_session_key_sha256: sessionKeyHash(typeof sessionKey === "string" ? sessionKey : undefined),
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
                const body = { response_body_state: bodyState };
                if (bodyState === "resource_refused") {
                    body.response_code_unit_count = codeUnits;
                }
                else if (bodyState === "unencodable") {
                    body.response_code_unit_count = codeUnits;
                    body.response_utf16_sha256 = utf16Sha256(text);
                }
                else {
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
            }
            else if (codeUnits > 131_072) {
                transportOutcome = "response_resource_refused";
                details.response_code_unit_count = codeUnits;
            }
            else if (hasUnpairedSurrogate(text)) {
                transportOutcome = "response_unencodable";
                details.response_code_unit_count = codeUnits;
                details.response_utf16_sha256 = utf16Sha256(text);
            }
            else {
                const encoded = Buffer.from(text, "utf8");
                if (encoded.byteLength > 65_536) {
                    transportOutcome = "response_oversize";
                    details.response_code_unit_count = codeUnits;
                    details.response_byte_count = encoded.byteLength;
                    details.response_sha256 = createHash("sha256").update(encoded).digest("hex");
                }
                else {
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
        }
        catch (error) {
            if (terminalSubmission !== undefined) {
                await terminalSubmission.catch(() => undefined);
            }
            else if (!closed) {
                await submit("provider_error", { failure_code: "host_llm_failed" }).catch(() => undefined);
            }
            return { kind: "stopped", reason: "manual_recovery" };
        }
        finally {
            if (timeout !== undefined)
                clearTimeout(timeout);
            if (!terminal.signal.aborted)
                terminal.abort("settled");
        }
    }
}
function aiFallbackKey(command, publicId) {
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
function requireAiPublicId(object, field, pattern) {
    const value = object[field];
    if (typeof value !== "string" || !pattern.test(value)) {
        throw new Error(`AI fallback result ${field} is invalid.`);
    }
    return value;
}
function isReceiptMetadata(value) {
    if (typeof value !== "object" || value === null || Array.isArray(value))
        return false;
    const metadata = value;
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
