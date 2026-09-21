import { spawn as nodeSpawn } from "node:child_process";
import { revalidatePythonExecutableForSpawn, } from "./config.js";
import { verifyCoreDistributionV1 } from "./core-distribution-v1.js";
import { expectedBridgeOperationId, MAX_RESPONSE_BYTES, parseBridgeResponse, safeModelEvaluationRefusalDetailsV1, serializeBridgeRequest, } from "./protocol.js";
import { FINANCE_DELIVERY_RECEIPT_PROOF_VERSION, financeDeliveryReceiptProofProvider, } from "./delivery-receipt-proof.js";
const DEFAULT_REAP_GRACE_MS = 1_000;
const CORE_CLI_BOOTSTRAP = [
    "import importlib.util,pathlib,runpy,sys",
    "root=pathlib.Path(sys.argv[1]).resolve(strict=True)",
    "package=(root/'finance_core').resolve(strict=True)",
    "sys.path.insert(0,str(root))",
    "top=importlib.util.find_spec('finance_core')",
    "module=importlib.util.find_spec('finance_core.openclaw_staging_bridge.cli')",
    "origins=[pathlib.Path(spec.origin).resolve(strict=True) for spec in (top,module) if spec is not None and spec.origin is not None]",
    "assert len(origins)==2 and all(package==path.parent or package in path.parents for path in origins)",
    "sys.argv=['finance_core.openclaw_staging_bridge.cli']",
    "runpy.run_module('finance_core.openclaw_staging_bridge.cli',run_name='__main__',alter_sys=True)",
].join(";");
const DELIVERY_RECEIPT_CLI_BOOTSTRAP = [
    "import importlib.util,pathlib,runpy,sys",
    "root=pathlib.Path(sys.argv[1]).resolve(strict=True)",
    "package=(root/'finance_core').resolve(strict=True)",
    "sys.path.insert(0,str(root))",
    "top=importlib.util.find_spec('finance_core')",
    "module=importlib.util.find_spec('finance_core.openclaw_staging_bridge.delivery_receipt_cli')",
    "origins=[pathlib.Path(spec.origin).resolve(strict=True) for spec in (top,module) if spec is not None and spec.origin is not None]",
    "assert len(origins)==2 and all(package==path.parent or package in path.parents for path in origins)",
    "sys.argv=['finance_core.openclaw_staging_bridge.delivery_receipt_cli']",
    "runpy.run_module('finance_core.openclaw_staging_bridge.delivery_receipt_cli',run_name='__main__',alter_sys=True)",
].join(";");
const BRIDGE_ERROR_CODES_BY_EXIT = new Map([
    [2, new Set(["WORKSPACE_MISSING"])],
    [3, new Set([
            "ENVELOPE_TOO_DEEP",
            "MALFORMED_ENVELOPE",
            "MISSING_IDEMPOTENCY_KEY",
            "OVERSIZED_ENVELOPE",
        ])],
    [4, new Set(["UNKNOWN_COMMAND"])],
    [5, new Set([
            "AI_FALLBACK_ARGUMENTS_REFUSED",
            "AI_FALLBACK_INTERNAL",
            "AI_FALLBACK_VALIDATION_REFUSED",
            "AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED",
            "AI_MODEL_CONFIG_REFUSED",
            "AI_MODEL_EVAL_REFUSED",
            "ARGUMENTS_REFUSED",
            "HANDOFF_NOT_FOUND",
            "HANDOFF_REFUSED",
            "INTAKE_NOT_FOUND",
            "NO_MATERIAL_CHANGE",
            "OCR_ENGINE_UNAVAILABLE",
            "PROPOSAL_NOT_FOUND",
            "TELEGRAM_SOURCE_REFUSED",
            "UNSUPPORTED_EDIT",
            "UNSUPPORTED_UPDATE_TYPE",
            "WORKSPACE_REFUSED",
        ])],
    [6, new Set([
            "ACTOR_MISMATCH",
            "AI_FALLBACK_CONFLICT",
            "AI_FALLBACK_NOT_ELIGIBLE",
            "AI_FALLBACK_NOT_FOUND",
            "AI_FALLBACK_POLICY_REFUSED",
            "AI_MODEL_COMPATIBILITY_CONFLICT",
            "AI_MODEL_COMPATIBILITY_FK_REFUSED",
            "AI_MODEL_COMPATIBILITY_POLICY_REFUSED",
            "AI_MODEL_CONFIG_NOT_ACCEPTED",
            "ATTACHMENT_EVIDENCE_NOT_FOUND",
            "CALLBACK_EXPIRED",
            "CALLBACK_KEY_MISSING",
            "CALLBACK_KEY_UNSAFE",
            "CALLBACK_TOKEN_INVALID",
            "CALLBACK_WRONG_ACTION",
            "FINALIZATION_LOCKED",
            "FINALIZATION_REFUSED",
            "HANDOFF_REFUSED",
            "HUMAN_ACTION_REFERENCE_INVALID",
            "HUMAN_ACTION_REFERENCE_REPLAYED",
            "IDEMPOTENCY_CONFLICT",
            "LIFECYCLE_CONFLICT",
            "OCR_EXTRACTION_FAILED",
            "PROPOSAL_TERMINAL_STATE",
            "PROPOSAL_UNAVAILABLE",
            "SNAPSHOT_AUTHORIZATION_REQUIRED",
            "STAGING_REFUSED",
            "STALE_CONTENT_HASH",
            "STALE_SNAPSHOT",
            "STALE_VERSION",
        ])],
    [7, new Set(["DEADLINE_EXCEEDED"])],
    [8, new Set([
            "AI_MODEL_COMPATIBILITY_INTERNAL",
            "HANDOFF_REFUSED",
            "INTERNAL_ERROR",
            "RESPONSE_TOO_LARGE",
        ])],
]);
const ALWAYS_RETRYABLE_BRIDGE_ERRORS = new Set([
    "FINALIZATION_LOCKED",
    "INTERNAL_ERROR",
    "STAGING_REFUSED",
    "WORKSPACE_MISSING",
]);
function isTrustedBridgeRefusal(exitCode, response) {
    const allowedCodes = BRIDGE_ERROR_CODES_BY_EXIT.get(exitCode);
    if (allowedCodes?.has(response.error.code) !== true)
        return false;
    if (response.error.code === "DEADLINE_EXCEEDED")
        return true;
    return response.error.retryable === ALWAYS_RETRYABLE_BRIDGE_ERRORS.has(response.error.code);
}
class BridgeProcessFailure extends Error {
    category;
    constructor(category) {
        super("Bridge process failed safely.");
        this.category = category;
    }
}
function processFailure(category) {
    return new BridgeProcessFailure(category);
}
export function bridgeProcessFailureCategory(error) {
    return error instanceof BridgeProcessFailure ? error.category : undefined;
}
async function revalidateRuntimeForSpawn(config) {
    await revalidatePythonExecutableForSpawn(config);
    await verifyCoreDistributionV1(config);
}
const spawnProcess = (executable, args, options) => nodeSpawn(executable, args, options);
function appendBounded(chunks, chunk, current, maximum) {
    const next = current + chunk.byteLength;
    if (next <= maximum)
        chunks.push(chunk);
    return next;
}
export class BridgeCliRunner {
    config;
    spawn;
    reapGraceMs;
    markUnhealthy;
    revalidateExecutable;
    receiptProofProvider;
    poisoned = false;
    constructor(config, spawn = spawnProcess, reapGraceMs = DEFAULT_REAP_GRACE_MS, markUnhealthy = () => undefined, revalidateExecutable = revalidateRuntimeForSpawn, receiptProofProvider = financeDeliveryReceiptProofProvider) {
        this.config = config;
        this.spawn = spawn;
        this.reapGraceMs = reapGraceMs;
        this.markUnhealthy = markUnhealthy;
        this.revalidateExecutable = revalidateExecutable;
        this.receiptProofProvider = receiptProofProvider;
    }
    async boundedReceiptPreparation(operation, deadlineMs) {
        let timer;
        try {
            return await Promise.race([
                operation,
                new Promise((_resolve, reject) => {
                    timer = setTimeout(() => reject(processFailure("BRIDGE_PROCESS_TIMEOUT")), deadlineMs);
                }),
            ]);
        }
        catch (error) {
            if (error instanceof BridgeProcessFailure)
                throw error;
            throw processFailure("BRIDGE_PROCESS_STARTUP_FAILED");
        }
        finally {
            if (timer !== undefined)
                clearTimeout(timer);
        }
    }
    async validateFinanceDeliveryReceiptCapability(deadlineMs) {
        if (!Number.isInteger(deadlineMs) || deadlineMs <= 0 || deadlineMs > 30_000) {
            throw new Error("Delivery receipt deadline must be an integer from 1 through 30000 milliseconds.");
        }
        await this.boundedReceiptPreparation(Promise.all([
            this.revalidateExecutable(this.config),
            this.receiptProofProvider.validate(this.config),
        ]).then(() => undefined), deadlineMs);
    }
    async recordFinanceDeliveryReceipt(material, deadlineMs) {
        if (this.poisoned)
            throw processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT");
        if (!Number.isInteger(deadlineMs) || deadlineMs <= 0 || deadlineMs > 30_000) {
            throw new Error("Delivery receipt deadline must be an integer from 1 through 30000 milliseconds.");
        }
        if (!Number.isSafeInteger(this.reapGraceMs) || this.reapGraceMs <= 0 ||
            this.reapGraceMs > DEFAULT_REAP_GRACE_MS) {
            throw new Error("Bridge reap grace is invalid.");
        }
        const expiresAt = performance.now() + deadlineMs;
        const receiptProofSha256 = await this.boundedReceiptPreparation(Promise.all([
            this.revalidateExecutable(this.config),
            this.receiptProofProvider.authenticate(this.config, material),
        ]).then(([, proof]) => proof), deadlineMs);
        const input = Buffer.from(JSON.stringify({
            workspace_path: this.config.workspaceRoot,
            attempt_nonce: material.attemptNonce,
            capability: material.capability,
            delivery_material_version: material.deliveryMaterialVersion,
            delivery_material_sha256: material.deliveryMaterialSha256,
            provider_message_id: material.providerMessageId,
            receipt_token_sha256: material.receiptTokenSha256,
            channel: material.channel,
            account_id: material.accountId,
            conversation_id: material.conversationId,
            session_key: material.sessionKey,
            source_identity_sha256: material.sourceIdentitySha256,
            receipt_proof_version: FINANCE_DELIVERY_RECEIPT_PROOF_VERSION,
            receipt_proof_sha256: receiptProofSha256,
        }));
        if (input.byteLength > 16_384)
            throw processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT");
        if (performance.now() >= expiresAt)
            throw processFailure("BRIDGE_PROCESS_TIMEOUT");
        const child = this.spawn(this.config.pythonExecutable, ["-I", "-B", "-c", DELIVERY_RECEIPT_CLI_BOOTSTRAP, this.config.coreDistributionRoot], {
            cwd: this.config.coreDistributionRoot,
            detached: false,
            env: {
                FINANCE_RUNTIME_ROOT: this.config.repoRoot,
                LANG: "C.UTF-8",
                LC_ALL: "C.UTF-8",
                PYTHONDONTWRITEBYTECODE: "1",
                PYTHONNOUSERSITE: "1",
                PYTHONUTF8: "1",
            },
            shell: false,
            stdio: ["pipe", "pipe", "pipe"],
        });
        await new Promise((resolve, reject) => {
            const stdout = [];
            let stdoutBytes = 0;
            let terminalError;
            let settled = false;
            let killTimer;
            let reapTimer;
            const clearTimers = () => {
                clearTimeout(timer);
                if (killTimer !== undefined)
                    clearTimeout(killTimer);
                if (reapTimer !== undefined)
                    clearTimeout(reapTimer);
            };
            const terminate = (error) => {
                if (terminalError !== undefined || settled)
                    return;
                terminalError = error;
                child.kill("SIGTERM");
                killTimer = setTimeout(() => {
                    if (settled)
                        return;
                    child.kill("SIGKILL");
                    reapTimer = setTimeout(() => {
                        if (settled)
                            return;
                        settled = true;
                        this.poisoned = true;
                        this.markUnhealthy();
                        clearTimers();
                        reject(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
                    }, this.reapGraceMs);
                }, this.reapGraceMs);
            };
            const timer = setTimeout(() => terminate(processFailure("BRIDGE_PROCESS_TIMEOUT")), Math.max(0, Math.floor(expiresAt - performance.now())));
            child.stdout.on("data", (value) => {
                const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
                stdoutBytes = appendBounded(stdout, chunk, stdoutBytes, 1_024);
                if (stdoutBytes > 1_024) {
                    terminate(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
                }
            });
            child.stderr.on("data", (value) => { void value; });
            child.stdin.on("error", () => {
                terminate(processFailure("BRIDGE_PROCESS_IO_FAILURE"));
            });
            child.once("error", () => {
                if (settled)
                    return;
                settled = true;
                clearTimers();
                reject(processFailure("BRIDGE_PROCESS_STARTUP_FAILED"));
            });
            child.once("close", (code, signal) => {
                if (settled)
                    return;
                settled = true;
                clearTimers();
                if (terminalError !== undefined) {
                    reject(terminalError);
                    return;
                }
                if (signal !== null || code !== 0) {
                    reject(processFailure("BRIDGE_PROCESS_NONZERO_UNVERIFIED"));
                    return;
                }
                try {
                    const response = JSON.parse(Buffer.concat(stdout).toString("utf8"));
                    if (typeof response !== "object" || response === null || Array.isArray(response) ||
                        Object.keys(response).sort().join(",") !== "observation_public_id,status" ||
                        response.status !== "ok" ||
                        typeof response.observation_public_id !== "string" ||
                        !/^d2dobs_[0-9a-f]{32}$/u.test(response.observation_public_id)) {
                        throw new Error("invalid response");
                    }
                    resolve();
                }
                catch {
                    reject(processFailure("BRIDGE_PROCESS_PROTOCOL_INVALID"));
                }
            });
            child.stdin.end(input);
        });
    }
    async run(request, deadlineMs, inheritedFd) {
        if (this.poisoned) {
            throw processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT");
        }
        if (!Number.isInteger(deadlineMs) || deadlineMs <= 0 || deadlineMs > 105_000) {
            throw new Error("Bridge deadline must be an integer from 1 through 105000 milliseconds.");
        }
        if (inheritedFd !== undefined && (!Number.isSafeInteger(inheritedFd) || inheritedFd < 0)) {
            throw new Error("Bridge inherited descriptor is invalid.");
        }
        if (!Number.isSafeInteger(this.reapGraceMs) || this.reapGraceMs <= 0 ||
            this.reapGraceMs > DEFAULT_REAP_GRACE_MS) {
            throw new Error("Bridge reap grace is invalid.");
        }
        const expiresAt = performance.now() + deadlineMs;
        let validationTimer;
        try {
            await Promise.race([
                this.revalidateExecutable(this.config),
                new Promise((_resolve, reject) => {
                    validationTimer = setTimeout(() => reject(processFailure("BRIDGE_PROCESS_TIMEOUT")), deadlineMs);
                }),
            ]);
        }
        catch (error) {
            if (error instanceof BridgeProcessFailure &&
                error.category === "BRIDGE_PROCESS_TIMEOUT") {
                throw error;
            }
            throw processFailure("BRIDGE_PROCESS_STARTUP_FAILED");
        }
        finally {
            if (validationTimer !== undefined)
                clearTimeout(validationTimer);
        }
        const input = serializeBridgeRequest(request);
        if (performance.now() >= expiresAt) {
            throw processFailure("BRIDGE_PROCESS_TIMEOUT");
        }
        const child = this.spawn(this.config.pythonExecutable, ["-I", "-B", "-c", CORE_CLI_BOOTSTRAP, this.config.coreDistributionRoot], {
            cwd: this.config.coreDistributionRoot,
            detached: false,
            env: {
                FINANCE_RUNTIME_ROOT: this.config.repoRoot,
                LANG: "C.UTF-8",
                LC_ALL: "C.UTF-8",
                PYTHONDONTWRITEBYTECODE: "1",
                PYTHONNOUSERSITE: "1",
                PYTHONUTF8: "1",
            },
            shell: false,
            stdio: inheritedFd === undefined
                ? ["pipe", "pipe", "pipe"]
                : ["pipe", "pipe", "pipe", inheritedFd],
        });
        return await new Promise((resolve, reject) => {
            const stdout = [];
            let stdoutBytes = 0;
            let terminalError;
            let settled = false;
            let killTimer;
            let reapTimer;
            const clearTimers = () => {
                clearTimeout(timer);
                if (killTimer !== undefined)
                    clearTimeout(killTimer);
                if (reapTimer !== undefined)
                    clearTimeout(reapTimer);
            };
            const terminate = (error) => {
                if (terminalError !== undefined || settled)
                    return;
                terminalError = error;
                child.kill("SIGTERM");
                killTimer = setTimeout(() => {
                    if (settled)
                        return;
                    child.kill("SIGKILL");
                    reapTimer = setTimeout(() => {
                        if (settled)
                            return;
                        settled = true;
                        this.poisoned = true;
                        this.markUnhealthy();
                        clearTimers();
                        reject(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
                    }, this.reapGraceMs);
                }, this.reapGraceMs);
            };
            const timer = setTimeout(() => {
                terminate(processFailure("BRIDGE_PROCESS_TIMEOUT"));
            }, Math.max(0, Math.floor(expiresAt - performance.now())));
            child.stdout.on("data", (value) => {
                const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
                stdoutBytes = appendBounded(stdout, chunk, stdoutBytes, MAX_RESPONSE_BYTES);
                if (stdoutBytes > MAX_RESPONSE_BYTES) {
                    terminate(processFailure("BRIDGE_PROCESS_RESOURCE_LIMIT"));
                }
            });
            child.stderr.on("data", (value) => {
                // Drain diagnostics so the pipe cannot block. Stderr is never trusted,
                // retained, or included in an exception returned to the caller.
                void value;
            });
            child.stdin.on("error", (error) => {
                if (settled)
                    return;
                void error;
                terminate(processFailure("BRIDGE_PROCESS_IO_FAILURE"));
            });
            child.once("error", (error) => {
                if (settled)
                    return;
                settled = true;
                clearTimers();
                void error;
                reject(processFailure("BRIDGE_PROCESS_STARTUP_FAILED"));
            });
            child.once("close", (code, signal) => {
                if (settled)
                    return;
                settled = true;
                clearTimers();
                if (terminalError !== undefined) {
                    reject(terminalError);
                    return;
                }
                if (signal !== null || code === null) {
                    reject(processFailure("BRIDGE_PROCESS_ABORTED"));
                    return;
                }
                let response;
                try {
                    response = parseBridgeResponse(Buffer.concat(stdout), request);
                }
                catch {
                    reject(processFailure(code !== 0 && stdoutBytes === 0
                        ? "BRIDGE_PROCESS_NONZERO_UNVERIFIED"
                        : "BRIDGE_PROCESS_PROTOCOL_INVALID"));
                    return;
                }
                if (code === 0) {
                    if (response.status !== "ok" ||
                        response.operationId !== expectedBridgeOperationId(request)) {
                        reject(processFailure("BRIDGE_PROCESS_PROTOCOL_INVALID"));
                        return;
                    }
                    resolve(response);
                    return;
                }
                if (response.status !== "error" ||
                    response.requestId !== request.request_id ||
                    response.operationId !== expectedBridgeOperationId(request) ||
                    !isTrustedBridgeRefusal(code, response)) {
                    reject(processFailure("BRIDGE_PROCESS_NONZERO_UNVERIFIED"));
                    return;
                }
                const safeDetails = safeModelEvaluationRefusalDetailsV1(response.error);
                resolve({
                    ...response,
                    error: {
                        code: response.error.code,
                        message: "Bridge command was refused.",
                        retryable: response.error.retryable,
                        ...(safeDetails === undefined ? {} : { details: safeDetails }),
                    },
                });
            });
            child.stdin.end(input);
        });
    }
}
