import { createBridgeRequest, framedDigest, humanDraftDeliveryAttemptId, humanDraftDeliveryKey, humanDraftObservationId, humanDraftObservationKey, humanActionIssuanceKey, humanActionRedemptionKey, } from "./protocol.js";
import { renderWholeCard } from "./whole-card.js";
export const DISABLED_REPLY = "Current action is not enabled.";
export const DISABLED_ACTIONS = [
    "edit-disabled",
];
export const ACTIVE_ACTIONS = ["confirm", "edit", "reject"];
export const ACTION_FAILURE_REPLY = "Finance action could not be applied safely. Request a fresh review.";
export const ACTION_OUTCOME_UNKNOWN_REPLY = "Finance decision outcome could not be verified. Finalization did not run; do not retry " +
    "from this card until the durable proposal status is checked.";
export const EDIT_PRESENTATION_FAILURE_REPLY = "Finance edit session was started, but the updated card could not be displayed safely. " +
    "Request the current Finance record.";
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
        replace: async (text, buttons) => await respond.editMessage({ text, buttons }),
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
function renderRedeemedDraftCard(value) {
    const card = value.human_draft_card;
    if (!isRecord(card) || typeof card.card_generation_public_id !== "string" ||
        !/^d1card_[0-9a-f]{32}$/u.test(card.card_generation_public_id) ||
        card.current_card_generation_public_id !== card.card_generation_public_id ||
        (card.completeness !== "complete" && card.completeness !== "incomplete") ||
        typeof card.confirm_available !== "boolean" || card.reject_available !== true ||
        card.confirm_available !== (card.completeness === "complete") ||
        typeof card.action_issue_batch_id !== "string" ||
        !/^[0-9a-f]{64}$/u.test(card.action_issue_batch_id) ||
        card.refusal_code !== null ||
        !["started", "accepted", "noop"].includes(String(card.operation_outcome)) ||
        !["not_issued", "issued"].includes(String(card.action_issuance_state)) ||
        card.final_transaction_created !== false || !isRecord(card.field_values) ||
        Object.keys(card.field_values).sort().join(",") !==
            "amount,category,currency,description,merchant,transaction_date" ||
        !Object.values(card.field_values).every((field) => typeof field === "string") ||
        !Array.isArray(card.unresolved_flags) || card.unresolved_flags.length > 32 ||
        !card.unresolved_flags.every((flag) => typeof flag === "string" &&
            /^[a-z0-9_]{1,100}$/u.test(flag))) {
        throw new Error("Redeemed D1 card is invalid.");
    }
    const proposalPublicId = card.completeness === "complete"
        ? card.proposal_public_id
        : card.decision_target_proposal_public_id;
    const proposalVersion = card.completeness === "complete"
        ? card.proposal_version
        : card.decision_target_proposal_version;
    const proposalContentHash = card.completeness === "complete"
        ? card.proposal_content_hash
        : card.decision_target_proposal_content_hash;
    if (typeof proposalPublicId !== "string" ||
        !/^(?:po_d1_[0-9a-f]{32}|prop_bridge_[0-9a-f]{32}|parser_output_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u
            .test(proposalPublicId) ||
        typeof proposalVersion !== "number" || !Number.isSafeInteger(proposalVersion) ||
        proposalVersion < 0 || typeof proposalContentHash !== "string" ||
        !/^[0-9a-f]{64}$/u.test(proposalContentHash) ||
        (card.completeness === "incomplete" &&
            (card.proposal_public_id !== null || card.proposal_version !== null ||
                card.proposal_content_hash !== null))) {
        throw new Error("Redeemed D1 action authority is invalid.");
    }
    return {
        actionIssueBatchId: card.action_issue_batch_id,
        cardReference: card.card_generation_public_id,
        confirmAvailable: card.confirm_available,
        proposalContentHash,
        proposalPublicId,
        proposalVersion,
        text: renderWholeCard({
            cardReference: card.card_generation_public_id,
            fields: card.field_values,
            language: "en",
            status: card.completeness === "complete" ? "publishable" : "incomplete",
            unresolvedReasons: card.unresolved_flags,
        }),
    };
}
function requireD1ActionButtons(result, card, includeEdit) {
    if (result.proposal_public_id !== card.proposalPublicId ||
        result.proposal_version !== card.proposalVersion ||
        result.content_hash !== card.proposalContentHash ||
        result.card_generation_public_id !== card.cardReference ||
        result.final_transaction_created !== false || !isRecord(result.actions)) {
        throw new Error("D1 human action issuance identity mismatch.");
    }
    const actions = result.actions;
    const expected = card.confirmAvailable
        ? ["confirm", "edit", "reject"]
        : ["reject"];
    const expectedSet = new Set(expected);
    if (Object.keys(actions).length !== expected.length ||
        Object.keys(actions).some((action) => !expectedSet.has(action))) {
        throw new Error("D1 human action reference set is invalid.");
    }
    const buttons = [];
    for (const action of expected) {
        const entry = actions[action];
        if (!isRecord(entry) || typeof entry.reference !== "string" ||
            !/^fha1_[A-Za-z0-9_-]{24}$/u.test(entry.reference) ||
            typeof entry.expiry !== "number" || !Number.isSafeInteger(entry.expiry)) {
            throw new Error("D1 human action reference is invalid.");
        }
        if (action !== "edit" || includeEdit) {
            buttons.push({
                text: action === "confirm" ? "Confirm" : action === "edit" ? "Edit" : "Reject",
                callback_data: humanActionCallbackData(action, entry.reference),
                ...(action === "confirm" ? { style: "success" }
                    : action === "reject" ? { style: "danger" } : {}),
            });
        }
    }
    return [buttons];
}
function redeemedD1Reference(material, context, binding) {
    const value = material.d1_decision_binding;
    if (value === undefined)
        return undefined;
    if (!isRecord(value) || Object.keys(value).sort().join(",") !==
        "authenticated_actor_id,card_generation_public_id,conversation_binding_id,reference_public_id,telegram_account_id,telegram_conversation_id" ||
        typeof value.reference_public_id !== "string" ||
        !/^haref_[0-9a-f]{32}$/u.test(value.reference_public_id) ||
        typeof value.card_generation_public_id !== "string" ||
        !/^d1card_[0-9a-f]{32}$/u.test(value.card_generation_public_id) ||
        value.authenticated_actor_id !== context.senderId ||
        value.telegram_account_id !== context.accountId ||
        value.telegram_conversation_id !== context.conversationId ||
        value.conversation_binding_id !== binding.bindingId) {
        throw new Error("Redeemed D1 decision binding is invalid.");
    }
    return value.reference_public_id;
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
        let durableD1Reference;
        let bindingId;
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
            durableD1Reference = redeemedD1Reference(material, context, bindingAfterRedemption);
            bindingId = bindingAfterRedemption.bindingId;
        }
        catch {
            await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
            return { handled: true };
        }
        if (context.action === "edit") {
            let card;
            let buttons;
            try {
                card = renderRedeemedDraftCard(material);
                const commandContext = {
                    workspace_path: current.workspaceRoot,
                    operator_actor_id: context.senderId,
                    telegram_account_id: context.accountId,
                    telegram_conversation_id: context.conversationId,
                    conversation_binding_id: bindingId,
                };
                const issued = await current.runner.run(createBridgeRequest("issue_human_actions", {
                    ...commandContext,
                    proposal_public_id: card.proposalPublicId,
                    reference_batch_id: card.actionIssueBatchId,
                    token_ttl_seconds: 600,
                    expected_proposal_version: card.proposalVersion,
                    expected_content_hash: card.proposalContentHash,
                    card_generation_public_id: card.cardReference,
                }, humanActionIssuanceKey(card.actionIssueBatchId)), 30_000);
                if (issued.status !== "ok") {
                    throw new Error("D1 human action issuance was refused.");
                }
                buttons = requireD1ActionButtons(issued.result, card, durableD1Reference === undefined);
                const attemptPublicId = humanDraftDeliveryAttemptId(card.cardReference, "replace");
                const begun = await current.runner.run(createBridgeRequest("begin_human_draft_card_delivery", {
                    ...commandContext,
                    card_generation_public_id: card.cardReference,
                    attempt_public_id: attemptPublicId,
                    delivery_material_hash: framedDigest("d1-card-delivery-material-v1", card.text, ...buttons.flat().map((button) => button.callback_data)),
                    transport_mode: "replace",
                    outbound_target_message_id: String(context.callbackMessageId),
                }, humanDraftDeliveryKey(attemptPublicId)), 30_000);
                if (begun.status !== "ok" || begun.result.attempt_public_id !== attemptPublicId) {
                    throw new Error("Human draft replacement attempt was not persisted.");
                }
                const observationPublicId = humanDraftObservationId(attemptPublicId, "initial");
                const observed = await current.runner.run(createBridgeRequest("record_human_draft_card_delivery_outcome", {
                    ...commandContext,
                    attempt_public_id: attemptPublicId,
                    observation_public_id: observationPublicId,
                    outcome: "unknown",
                    error_code: null,
                    outbound_message_id: null,
                    trusted_receipt_hash: null,
                }, humanDraftObservationKey(observationPublicId)), 30_000);
                if (observed.status !== "ok" ||
                    observed.result.observation_public_id !== observationPublicId) {
                    throw new Error("Human draft replacement observation was not persisted.");
                }
            }
            catch {
                await context.reply(ACTION_FAILURE_REPLY).catch(() => undefined);
                return { handled: true };
            }
            try {
                await context.replace(card.text, buttons);
            }
            catch {
                await context.reply(EDIT_PRESENTATION_FAILURE_REPLY).catch(() => undefined);
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
                ...(durableD1Reference === undefined
                    ? {}
                    : { d1_reference_public_id: durableD1Reference }),
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
            await context.replace(persistedReply, []);
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
