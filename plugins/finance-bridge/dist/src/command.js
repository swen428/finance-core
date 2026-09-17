import { createBridgeRequest, } from "./protocol.js";
import { parseProcessingStatusV2, renderProcessingStatusDetailV2, } from "./processing-status-v2.js";
const COMMAND_DEADLINE_MS = 30_000;
const PROPOSAL_ID = /^prop_bridge_[0-9a-f]{32}$/u;
const INTAKE_ID = /^(?:raw_intake_bridge_[0-9a-f]{32}|raw_intake_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/u;
const HASH = /^[0-9a-f]{64}$/u;
const SAFE_FILENAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$/u;
const SAFE_ID = /^[A-Za-z0-9_-]{1,200}$/u;
const MONEY_DISPLAY = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/u;
const RECEIPT_STATES = new Set([
    "confirmed_incomplete",
    "prepared_pending_authorization",
    "authorized_pending_finalization",
    "finalized",
]);
class ReceiptOutcomeUnknownError extends Error {
    proposalPublicId;
    constructor(proposalPublicId) {
        super("Receipt command outcome is unknown.");
        this.proposalPublicId = proposalPublicId;
    }
}
function senderFromBinding(binding) {
    if (typeof binding.data !== "object" || binding.data === null || Array.isArray(binding.data)) {
        return undefined;
    }
    const sender = binding.data.senderId;
    return typeof sender === "string" ? sender : undefined;
}
function isPrivateOwnerContext(context) {
    return context.channel === "telegram" && context.isAuthorizedSender &&
        context.senderIsOwner === true && context.senderId !== undefined &&
        context.gatewayClientScopes === undefined &&
        context.accountId !== undefined && context.messageThreadId === undefined &&
        context.threadParentId === undefined && context.from !== undefined &&
        context.to !== undefined && context.from === context.to;
}
function matchesCurrentContext(binding, context) {
    return binding.pluginId === "finance-bridge" && binding.channel === "telegram" &&
        binding.accountId === context.accountId && binding.conversationId === context.senderId &&
        senderFromBinding(binding) === context.senderId && binding.threadId === undefined &&
        (binding.parentConversationId === undefined ||
            binding.parentConversationId === binding.conversationId);
}
function response(text) {
    return { text, continueAgent: false };
}
function isRuntime(value) {
    return typeof value === "object" && value !== null &&
        typeof value.workspaceRoot === "string" && value.workspaceRoot.length > 0 &&
        typeof value.runner === "object" && value.runner !== null &&
        typeof value.runner.run === "function";
}
function requireExactResult(result, fields) {
    const expected = new Set(fields);
    const keys = Object.keys(result);
    if (keys.length !== expected.size || keys.some((key) => !expected.has(key))) {
        throw new Error("Bridge result does not match the direct-command contract.");
    }
}
function requireString(result, field, pattern = SAFE_ID) {
    const value = result[field];
    if (typeof value !== "string" || !pattern.test(value)) {
        throw new Error(`Bridge result ${field} is invalid.`);
    }
    return value;
}
function requireHash(result, field) {
    return requireString(result, field, HASH);
}
function requireNonNegativeInteger(result, field) {
    const value = result[field];
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
        throw new Error(`Bridge result ${field} is invalid.`);
    }
    return value;
}
function requireMoneyDisplay(result, field) {
    return requireString(result, field, MONEY_DISPLAY);
}
function requireOk(response_) {
    if (response_.status !== "ok")
        throw new Error("Bridge command refused.");
    return response_.result;
}
function requireStatusBinding(result, proposalPublicId) {
    if (result.identity_kind !== "proposal" ||
        requireString(result, "proposal_public_id", PROPOSAL_ID) !== proposalPublicId ||
        result.parse_status !== "confirmed") {
        throw new Error("Proposal review is not confirmed for this direct receipt command.");
    }
    return {
        proposalVersion: requireNonNegativeInteger(result, "proposal_version"),
        contentHash: requireHash(result, "effective_content_hash"),
    };
}
function tokenize(args) {
    const tokens = args?.trim().split(/\s+/u).filter((item) => item.length > 0) ?? [];
    if (tokens.length === 0)
        return undefined;
    const [rawVerb, ...values] = tokens;
    return { verb: rawVerb.toLowerCase(), values };
}
function requireProposal(value) {
    if (value === undefined || !PROPOSAL_ID.test(value)) {
        throw new Error("Receipt proposal identity is invalid.");
    }
    return value;
}
function requireSnapshotHash(value) {
    if (value === undefined || !HASH.test(value))
        throw new Error("Snapshot hash is invalid.");
    return value;
}
function requireCommandFilename(value) {
    if (value === undefined || !SAFE_FILENAME.test(value)) {
        throw new Error("Fact-set filename is invalid.");
    }
    return value;
}
async function readStatusBinding(runtime, proposalPublicId) {
    const status = requireOk(await runtime.runner.run(createBridgeRequest("get_status", {
        workspace_path: runtime.workspaceRoot,
        proposal_public_id: proposalPublicId,
    }), COMMAND_DEADLINE_MS));
    return requireStatusBinding(status, proposalPublicId);
}
async function renderReceiptOperation(proposalPublicId, invoke, render) {
    try {
        const bridgeResponse = await invoke();
        if (bridgeResponse.status === "error") {
            return response(bridgeResponse.error.retryable
                ? `Receipt command was temporarily refused. Run /finance receipt-status ${proposalPublicId} and retry.`
                : `Receipt command was refused. Run /finance receipt-status ${proposalPublicId} before another action.`);
        }
        return response(render(bridgeResponse.result));
    }
    catch {
        // A conversion, fact-set, authorization, finalization, or calculation
        // snapshot may have persisted before its response became unverifiable.
        // The only safe recovery surface is durable status, never a retry claim.
        throw new ReceiptOutcomeUnknownError(proposalPublicId);
    }
}
function renderPrepared(result, proposalPublicId) {
    requireExactResult(result, [
        "identity_kind", "proposal_public_id", "receipt_public_id", "conversion_command_public_id",
        "conversion_result_hash", "content_hash",
    ]);
    if (result.identity_kind !== "prepare_receipt_completion" ||
        requireString(result, "proposal_public_id", PROPOSAL_ID) !== proposalPublicId) {
        throw new Error("Prepared receipt identity is invalid.");
    }
    const receipt = requireString(result, "receipt_public_id");
    const command = requireString(result, "conversion_command_public_id");
    const conversionHash = requireHash(result, "conversion_result_hash");
    requireHash(result, "content_hash");
    return [
        `Receipt ${receipt} is prepared.`,
        `Conversion ${command} is ready for a human fact-set file.`,
        `Conversion hash: ${conversionHash}`,
    ].join("\n");
}
function renderFactSet(result) {
    requireExactResult(result, [
        "identity_kind", "receipt_public_id", "fact_set_public_id", "fact_set_version",
        "fact_set_result_hash", "item_count", "allocation_count",
    ]);
    if (result.identity_kind !== "apply_fact_set")
        throw new Error("Fact-set result kind is invalid.");
    const factSet = requireString(result, "fact_set_public_id");
    requireNonNegativeInteger(result, "fact_set_version");
    requireHash(result, "fact_set_result_hash");
    requireNonNegativeInteger(result, "item_count");
    requireNonNegativeInteger(result, "allocation_count");
    return `Human fact set ${factSet} was persisted. Review its calculation snapshot next.`;
}
function renderSnapshotReview(result, proposalPublicId) {
    requireExactResult(result, [
        "identity_kind", "proposal_public_id", "receipt_public_id", "fact_set_public_id",
        "fact_set_version", "fact_set_result_hash", "calculation_snapshot_id",
        "calculation_snapshot_hash", "currency", "payer_participant_public_id", "total_paid",
        "total_to_collect", "participant_shares", "settlement_obligations",
    ]);
    if (result.identity_kind !== "finalization_snapshot_review" ||
        requireString(result, "proposal_public_id", PROPOSAL_ID) !== proposalPublicId) {
        throw new Error("Snapshot review identity is invalid.");
    }
    const currency = requireString(result, "currency", /^[A-Z]{3}$/u);
    const payer = requireString(result, "payer_participant_public_id");
    const totalPaid = requireMoneyDisplay(result, "total_paid");
    const totalToCollect = requireMoneyDisplay(result, "total_to_collect");
    const shares = result.participant_shares;
    if (typeof shares !== "object" || shares === null || Array.isArray(shares) ||
        Object.keys(shares).length !== 1 || shares[payer] === undefined ||
        typeof shares[payer] !== "string" || !MONEY_DISPLAY.test(shares[payer]) ||
        result.settlement_obligations === undefined || !Array.isArray(result.settlement_obligations) ||
        result.settlement_obligations.length !== 0) {
        throw new Error("Snapshot review output is not safe to render.");
    }
    const snapshotId = requireString(result, "calculation_snapshot_id");
    const snapshotHash = requireHash(result, "calculation_snapshot_hash");
    const receipt = requireString(result, "receipt_public_id");
    const factSet = requireString(result, "fact_set_public_id");
    requireNonNegativeInteger(result, "fact_set_version");
    requireHash(result, "fact_set_result_hash");
    return [
        `Receipt ${receipt}`,
        `Snapshot ${snapshotId} for fact set ${factSet}`,
        `Currency: ${currency}`,
        `Total paid: ${totalPaid}`,
        `Payer ${payer} share: ${shares[payer]}`,
        `Total to collect: ${totalToCollect}`,
        `Settlement obligations: none`,
        `Review hash: ${snapshotHash}`,
    ].join("\n");
}
function renderAuthorization(result, expectedHash) {
    requireExactResult(result, [
        "identity_kind", "receipt_public_id", "authorization_id", "authorization_content_hash",
        "calculation_snapshot_id", "calculation_snapshot_hash",
    ]);
    if (result.identity_kind !== "authorize_finalization" ||
        requireHash(result, "calculation_snapshot_hash") !== expectedHash) {
        throw new Error("Finalization authorization does not match the reviewed snapshot.");
    }
    const authorization = requireString(result, "authorization_id");
    requireHash(result, "authorization_content_hash");
    return `Finalization authorization ${authorization} is recorded. Run finalize separately.`;
}
function renderFinalization(result, proposalPublicId) {
    requireExactResult(result, [
        "identity_kind", "path", "proposal_public_id", "confirmation_public_id", "receipt_public_id",
        "fact_set_public_id", "fact_set_version", "calculation_snapshot_id",
        "calculation_snapshot_hash", "authorization_id", "finalization_public_id",
        "transaction_public_id", "final_transaction_created", "content_hash",
    ]);
    if (result.identity_kind !== "finalize" || result.path !== "receipt" ||
        requireString(result, "proposal_public_id", PROPOSAL_ID) !== proposalPublicId ||
        result.final_transaction_created !== true) {
        throw new Error("Receipt finalization result is invalid.");
    }
    const transaction = requireString(result, "transaction_public_id");
    requireString(result, "finalization_public_id");
    requireHash(result, "calculation_snapshot_hash");
    requireHash(result, "content_hash");
    return `Receipt finalization is durable. Transaction ${transaction} was created.`;
}
function renderStatus(result, proposalPublicId) {
    if (result.identity_kind !== "proposal" ||
        requireString(result, "proposal_public_id", PROPOSAL_ID) !== proposalPublicId ||
        typeof result.final_transaction_created !== "boolean" ||
        typeof result.finalization_state !== "string" || !RECEIPT_STATES.has(result.finalization_state)) {
        throw new Error("Receipt status result is invalid.");
    }
    return `Receipt status: ${result.finalization_state}.`;
}
async function handleFinanceCommand(context, availability) {
    const parsed = tokenize(context.args);
    if (parsed === undefined)
        return response("Use /finance bind, status, or unbind.");
    const { verb, values } = parsed;
    const legacy = new Set(["bind", "status", "unbind"]);
    const receipt = new Set([
        "prepare-receipt", "apply-facts", "review-snapshot", "authorize", "finalize", "receipt-status",
    ]);
    if (!legacy.has(verb) && !receipt.has(verb)) {
        return response("Use /finance bind, status, or unbind. Receipt commands require a bound private conversation.");
    }
    if (!isPrivateOwnerContext(context)) {
        return response("Finance bridge is available only in the authorized private direct message.");
    }
    const currentAvailability = availability();
    if (currentAvailability !== true && !isRuntime(currentAvailability)) {
        return response("Finance bridge is not available.");
    }
    if ((verb === "bind" || verb === "unbind") && values.length !== 0) {
        return response("Use /finance bind, status, or unbind.");
    }
    if (verb === "status" && values.length > 1) {
        return response("Use /finance status or /finance status <intake_ref>.");
    }
    if (verb === "bind") {
        if (context.senderId !== context.to?.replace(/^telegram:/u, "")) {
            return response("Finance bridge is available only in the authorized private direct message.");
        }
        const request = await context.requestConversationBinding({
            summary: "Finance deterministic staging bridge",
            detachHint: "/finance unbind",
            data: { senderId: context.senderId },
        });
        if (request.status === "pending")
            return { ...request.reply, continueAgent: false };
        if (request.status === "error")
            return response("Finance binding request was refused.");
        if (!matchesCurrentContext(request.binding, context)) {
            return response("Finance binding identity did not match the private conversation.");
        }
        return response("Bound this private conversation to the Finance staging bridge.");
    }
    if (verb === "status") {
        const current = await context.getCurrentConversationBinding();
        const bound = current !== null && matchesCurrentContext(current, context);
        if (values.length === 0) {
            return response(bound
                ? "Finance bridge is bound to this private conversation."
                : "Finance bridge is not bound to this private conversation.");
        }
        if (!bound)
            return response("Finance bridge is not bound to this private conversation.");
        if (!isRuntime(currentAvailability))
            return response("Finance bridge is not available.");
        const intakePublicId = values[0];
        if (!INTAKE_ID.test(intakePublicId))
            return response("Finance intake reference is invalid.");
        const status = requireOk(await currentAvailability.runner.run(createBridgeRequest("get_ai_processing_status_v2", {
            workspace_path: currentAvailability.workspaceRoot,
            intake_public_id: intakePublicId,
        }), COMMAND_DEADLINE_MS));
        return response(renderProcessingStatusDetailV2(parseProcessingStatusV2(status)));
    }
    const current = await context.getCurrentConversationBinding();
    if (current === null || !matchesCurrentContext(current, context)) {
        return response("Finance bridge is not bound to this private conversation.");
    }
    if (verb === "unbind") {
        const detached = await context.detachConversationBinding();
        return response(detached.removed
            ? "Finance bridge binding removed."
            : "Finance bridge binding was not removed.");
    }
    if (!isRuntime(currentAvailability))
        return response("Finance bridge is not available.");
    const runtime = currentAvailability;
    const operatorActorId = context.senderId;
    const expectedArgumentCount = verb === "apply-facts" || verb === "authorize" ? 2 : 1;
    if (values.length !== expectedArgumentCount) {
        throw new Error("Receipt command arguments are invalid.");
    }
    const proposalPublicId = requireProposal(values[0]);
    const filename = verb === "apply-facts" ? requireCommandFilename(values[1]) : undefined;
    const expectedHash = verb === "authorize" ? requireSnapshotHash(values[1]) : undefined;
    const requireReview = verb === "prepare-receipt" || verb === "finalize";
    const binding = requireReview
        ? await readStatusBinding(runtime, proposalPublicId)
        : undefined;
    const base = {
        workspace_path: runtime.workspaceRoot,
        proposal_public_id: proposalPublicId,
        operator_actor_id: operatorActorId,
    };
    if (verb === "prepare-receipt") {
        return renderReceiptOperation(proposalPublicId, () => runtime.runner.run(createBridgeRequest("prepare_receipt_completion", { ...base, proposal_version: binding.proposalVersion, content_hash: binding.contentHash }, `bridge-prepare-receipt:${proposalPublicId}`), COMMAND_DEADLINE_MS), (result) => renderPrepared(result, proposalPublicId));
    }
    if (verb === "apply-facts") {
        return renderReceiptOperation(proposalPublicId, () => runtime.runner.run(createBridgeRequest("apply_fact_set", { ...base, command_filename: filename }, `bridge-apply-fact-set:${proposalPublicId}`), COMMAND_DEADLINE_MS), renderFactSet);
    }
    if (verb === "review-snapshot") {
        return renderReceiptOperation(proposalPublicId, () => runtime.runner.run(createBridgeRequest("get_finalization_snapshot_review", base, `bridge-finalization-snapshot-review:${proposalPublicId}`), COMMAND_DEADLINE_MS), (result) => renderSnapshotReview(result, proposalPublicId));
    }
    if (verb === "authorize") {
        return renderReceiptOperation(proposalPublicId, () => runtime.runner.run(createBridgeRequest("authorize_finalization", { ...base, expected_calculation_snapshot_hash: expectedHash }, `bridge-authorize-finalization:${proposalPublicId}`), COMMAND_DEADLINE_MS), (result) => renderAuthorization(result, expectedHash));
    }
    if (verb === "finalize") {
        return renderReceiptOperation(proposalPublicId, () => runtime.runner.run(createBridgeRequest("finalize", {
            ...base,
            proposal_version: binding.proposalVersion,
            content_hash: binding.contentHash,
            receipt_only: true,
        }, `bridge-finalize:${proposalPublicId}`), COMMAND_DEADLINE_MS), (result) => renderFinalization(result, proposalPublicId));
    }
    const result = requireOk(await runtime.runner.run(createBridgeRequest("get_status", { workspace_path: runtime.workspaceRoot, proposal_public_id: proposalPublicId }), COMMAND_DEADLINE_MS));
    return response(renderStatus(result, proposalPublicId));
}
export function createFinanceCommand(availability) {
    return {
        name: "finance",
        description: "Manage the deterministic Finance staging bridge and direct receipt completion.",
        channels: ["telegram"],
        acceptsArgs: true,
        requireAuth: true,
        requiredScopes: ["operator.write"],
        exposeSenderIsOwner: true,
        handler: async (context) => {
            try {
                return await handleFinanceCommand(context, availability);
            }
            catch (error) {
                if (error instanceof ReceiptOutcomeUnknownError) {
                    return response(`Receipt command outcome is unknown. Run /finance receipt-status ${error.proposalPublicId}.`);
                }
                return response("Finance bridge is not available.");
            }
        },
    };
}
