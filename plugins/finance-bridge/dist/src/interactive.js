import { createBridgeRequest, humanActionRedemptionKey, } from "./protocol.js";
export const DISABLED_REPLY = "Current action is not enabled.";
export const DISABLED_ACTIONS = [
    "edit-disabled",
];
export const ACTIVE_ACTIONS = ["confirm", "edit", "reject"];
export const ACTION_FAILURE_REPLY = "Finance action could not be applied safely. Request a fresh review.";
export const ACTION_OUTCOME_UNKNOWN_REPLY = "Finance decision outcome could not be verified. Finalization did not run; do not retry " +
    "from this card until the durable proposal status is checked.";
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isDisabledAction(value) {
    return typeof value === "string" && DISABLED_ACTIONS.includes(value);
}
function isCanonicalTelegramPrivateId(value) {
    return typeof value === "string" && value.length <= 32 && /^[1-9][0-9]*$/u.test(value);
}
function isCanonicalHostIdentifier(value) {
    return typeof value === "string" && /^[\x21-\x7e]{1,200}$/u.test(value);
}
function parseActivePayload(value) {
    if (typeof value !== "string")
        return undefined;
    const match = /^(confirm|edit|reject):(fha1_[A-Za-z0-9_-]{24})$/u.exec(value);
    if (match === null)
        return undefined;
    return { action: match[1], reference: match[2] };
}
function narrowContext(value) {
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
        payload: value.callback.payload,
        reply: async (text) => await respond.reply({ text }),
        currentBinding: value.getCurrentConversationBinding,
    };
}
function bindingMatches(context, binding) {
    if (!isCanonicalTelegramPrivateId(context.senderId) ||
        context.conversationId !== context.senderId ||
        !isCanonicalHostIdentifier(context.accountId) ||
        !isCanonicalHostIdentifier(binding.bindingId) ||
        binding.pluginId !== "finance-bridge" || binding.channel !== "telegram" ||
        binding.accountId !== context.accountId || binding.conversationId !== context.conversationId ||
        (binding.parentConversationId !== undefined &&
            binding.parentConversationId !== binding.conversationId) || binding.threadId !== undefined ||
        !isRecord(binding.data))
        return false;
    return binding.data.senderId === context.senderId;
}
function activeBindingMatches(context, binding) {
    return bindingMatches(context, binding);
}
export function disabledCallbackData(action) {
    const data = `finance-bridge:${action}`;
    if (Buffer.byteLength(data, "utf8") > 64)
        throw new Error("Callback data exceeds 64 bytes.");
    return data;
}
export function humanActionCallbackData(action, reference) {
    if (!/^fha1_[A-Za-z0-9_-]{24}$/u.test(reference)) {
        throw new Error("Human action reference is invalid.");
    }
    const data = `finance-bridge:${action}:${reference}`;
    if (Buffer.byteLength(data, "utf8") > 64)
        throw new Error("Callback data exceeds 64 bytes.");
    return data;
}
function narrowActiveContext(value) {
    if (!isRecord(value) || value.channel !== "telegram" || value.isGroup !== false ||
        value.isForum !== false || value.threadId !== undefined ||
        (value.parentConversationId !== undefined &&
            value.parentConversationId !== value.conversationId) || !isRecord(value.auth) ||
        value.auth.isAuthorizedSender !== true || !isCanonicalHostIdentifier(value.accountId) ||
        typeof value.callbackId !== "string" || value.callbackId.length === 0 ||
        value.callbackId.length > 200 || !isCanonicalTelegramPrivateId(value.conversationId) ||
        !isCanonicalTelegramPrivateId(value.senderId) || value.senderId !== value.conversationId ||
        !isRecord(value.callback) || value.callback.namespace !== "finance-bridge" ||
        typeof value.callback.messageId !== "number" || !Number.isSafeInteger(value.callback.messageId) ||
        value.callback.messageId <= 0 || value.callback.chatId !== value.conversationId ||
        !isRecord(value.respond) || typeof value.respond.reply !== "function" ||
        typeof value.respond.editMessage !== "function" ||
        typeof value.getCurrentConversationBinding !== "function")
        return undefined;
    const parsed = parseActivePayload(value.callback.payload);
    if (parsed === undefined ||
        value.callback.data !== humanActionCallbackData(parsed.action, parsed.reference)) {
        return undefined;
    }
    const respond = value.respond;
    return {
        accountId: value.accountId,
        callbackId: value.callbackId,
        callbackMessageId: value.callback.messageId,
        conversationId: value.conversationId,
        senderId: value.senderId,
        action: parsed.action,
        reference: parsed.reference,
        reply: async (text) => await respond.reply({ text }),
        replace: async (text) => await respond.editMessage({ text }),
        currentBinding: value.getCurrentConversationBinding,
    };
}
function requireString(result, field) {
    const value = result[field];
    if (typeof value !== "string" || value.length === 0) {
        throw new Error(`Human action ${field} is invalid.`);
    }
    return value;
}
function requireInteger(result, field) {
    const value = result[field];
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
        throw new Error(`Human action ${field} is invalid.`);
    }
    return value;
}
export function createHumanActionInteractiveHandler(runtime) {
    return async (value) => {
        const disabled = narrowContext(value);
        if (disabled !== undefined) {
            try {
                const binding = await disabled.currentBinding();
                if (binding !== null && bindingMatches(disabled, binding))
                    await disabled.reply(DISABLED_REPLY);
            }
            catch { /* fail closed */ }
            return { handled: true };
        }
        const context = narrowActiveContext(value);
        if (context === undefined)
            return { handled: true };
        let current;
        let material;
        let proposal;
        let decisionKey;
        try {
            const binding = await context.currentBinding();
            const available = runtime();
            if (binding === null || !activeBindingMatches(context, binding) || available === undefined) {
                return { handled: true };
            }
            current = available;
            const redemption = await current.runner.run(createBridgeRequest("redeem_human_action", {
                workspace_path: current.workspaceRoot,
                short_reference: context.reference,
                action: context.action,
                operator_actor_id: context.senderId,
                telegram_account_id: context.accountId,
                telegram_conversation_id: context.conversationId,
                conversation_binding_id: binding.bindingId,
                callback_id: context.callbackId,
                callback_message_id: context.callbackMessageId,
            }, humanActionRedemptionKey(context.callbackId)), 30_000);
            if (redemption.status !== "ok")
                throw new Error("Human action redemption refused.");
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
        }
        catch {
            await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
            return { handled: true };
        }
        if (context.action === "edit") {
            let session;
            try {
                session = requireString(material, "guided_edit_session_public_id");
                if (!/^gedit_[0-9a-f]{32}$/u.test(session)) {
                    throw new Error("Guided edit session identity is invalid.");
                }
            }
            catch {
                await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
                return { handled: true };
            }
            const prompt = "Finance edit session started. Reply with one field per message, for example:\n" +
                "金额=321.89\n币种=SGD\n日期=2026-09-03\n商户=Example\n描述=Lunch\n分类=Meals\n" +
                "Reply 完成 when finished. Account and natural-language recalculation are not enabled.";
            try {
                await context.replace(prompt);
            }
            catch {
                await context.reply(prompt).catch(() => undefined);
            }
            return { handled: true };
        }
        let decision;
        try {
            decision = await current.runner.run(createBridgeRequest(context.action, {
                workspace_path: current.workspaceRoot,
                proposal_public_id: proposal,
                operator_actor_id: context.senderId,
                proposal_version: requireInteger(material, "proposal_version"),
                content_hash: requireString(material, "content_hash"),
                callback_token: requireString(material, "callback_token"),
                callback_expiry: requireInteger(material, "callback_expiry"),
            }, decisionKey), 30_000);
        }
        catch {
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
        }
        catch {
            await context.reply(ACTION_OUTCOME_UNKNOWN_REPLY).catch(() => undefined);
            return { handled: true };
        }
        const persistedReply = context.action === "confirm"
            ? "Finance proposal confirmed. Finalization has not run."
            : "Finance proposal rejected. No final transaction was created.";
        try {
            await context.replace(persistedReply);
        }
        catch {
            await context.reply(`${persistedReply} The Telegram review card could not be updated.`).catch(() => undefined);
        }
        return { handled: true };
    };
}
export function createDisabledInteractiveHandler(_privateController) {
    return async (value) => {
        const context = narrowContext(value);
        if (context === undefined)
            return { handled: true };
        try {
            const binding = await context.currentBinding();
            if (binding === null || !bindingMatches(context, binding))
                return { handled: true };
            await context.reply(DISABLED_REPLY);
        }
        catch {
            return { handled: true };
        }
        return { handled: true };
    };
}
