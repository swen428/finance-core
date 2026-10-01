/** Fixed synthetic Core cuts. No Host/Bridge export or publication. */
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { closeSync, constants, fstatSync, fsyncSync, lstatSync, read, readFileSync } from "node:fs";
import { join } from "node:path";
import { revalidatePythonExecutableForSpawn } from "./config.js";
import { verifyCoreDistributionV1 } from "./core-distribution-v1.js";
import { openProfileGate } from "./profile-gate.js";
import { checkProfileAncestors, profileLocatorEnvironment } from "./profile-layout.js";
import { createDirectoryExclusiveAt, openDirectory, openExistingDirectoryAt, openFileAt, rejectAclGrants, listAt, } from "./posix.js";
const VERSION = "delegated-cut-worker-v1";
const BUNDLE_VERSION = "delegated-cut-bundle-v1";
const BUNDLE_SCOPE = "core_committed_snapshot";
const REGISTRY_VERSION = "core-attachment-reference-registry-v1";
const BUNDLE_LIMITS_VERSION = "core-snapshot-bundle-limits-v1";
const BUNDLE_LIMITS = Object.freeze({
    max_core_db_bytes: 67_108_864,
    max_db_stage_bytes: 134_217_728,
    max_attachment_bytes: 20_971_520,
    max_attachment_members: 4_096,
    max_references: 65_536,
    max_manifest_bytes: 1_048_576,
    max_stage_bytes: 268_435_456,
    min_free_bytes: 67_108_864,
    backup_pages_per_step: 256,
});
const MAX_FRAME = 8192;
const MAX_HOLD_MS = 30_000;
const GATE_CANCEL_POLL_MS = 100;
const REAP_GRACE_MS = 1_000;
const HASH = /^[0-9a-f]{64}$/u;
const HEX32 = /^[0-9a-f]{32}$/u;
const DECIMAL = /^(?:0|[1-9][0-9]*)$/u;
const UTC_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/u;
const BUNDLE_TERMINAL_COMMON = ["version", "type", "cut_id", "worker_id", "profile_id",
    "operation", "artifact_sha256", "schema_sha256", "scope", "registry_version",
    "limits_version", "core_version", "core_api_contract_version"];
const BUNDLE_STAGED_FIELDS = ["bundle_stage_dev", "bundle_stage_ino", "db_stage_dev",
    "db_stage_ino", "db_output_dev", "db_output_ino", "db_bytes", "db_sha256",
    "db_page_count", "manifest_sha256", "manifest_bytes", "member_count", "member_bytes",
    "reference_count", "reference_sha256", "snapshot_recorded_at", "package_completed_at",
    "schema_fingerprint", "migration_ledger_sha256", "migration_ledger_count"];
const SHARD = /^[0-9a-f]{2}$/u;
const ATTACHMENT_MEMBER = /^[0-9a-f]{64}\.(?:jpg|png|pdf)$/u;
const BOOTSTRAP = [
    "import importlib.util,pathlib,runpy,sys",
    "root=pathlib.Path(sys.argv[1]).resolve(strict=True)",
    "package=(root/'finance_core').resolve(strict=True)",
    "sys.path.insert(0,str(root))",
    "top=importlib.util.find_spec('finance_core')",
    "name='finance_core.'+sys.argv[2]",
    "module=importlib.util.find_spec(name)",
    "origins=[pathlib.Path(spec.origin).resolve(strict=True) for spec in (top,module) if spec is not None and spec.origin is not None]",
    "assert len(origins)==2 and all(package==path.parent or package in path.parents for path in origins)",
    "sys.argv=[name]",
    "runpy.run_module(name,run_name='__main__',alter_sys=True)",
].join(";");
// An unresolved close cannot be converted into lease release by a timer.
const uncertainCuts = new Set();
let unhealthy = false;
function releaseContext(context) {
    if (context.released)
        return;
    // Child close/reap (or a pre-spawn failure) is the only caller path.
    closeSync(context.stageFd);
    context.lease.close();
    context.gate.close();
    closeSync(context.workFd);
    closeSync(context.profileFd);
    context.released = true;
}
function exactObject(value, keys) {
    if (typeof value !== "object" || value === null || Array.isArray(value) ||
        Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) {
        throw new Error("Managed cut protocol frame is invalid.");
    }
    return value;
}
/** Bundle control frames are small, flat/nested JSON objects; reject duplicate
 * keys and non-canonical numbers before JSON.parse can erase the evidence. */
function strictBundleFrame(bytes) {
    const source = bytes.toString("utf8");
    if (!Buffer.from(source, "utf8").equals(bytes))
        throw new Error("Invalid UTF-8 frame.");
    let index = 0;
    const skipSpace = () => { while (/\s/u.test(source[index] ?? ""))
        index += 1; };
    const parseString = () => {
        const begin = index;
        if (source[index++] !== '"')
            throw new Error("Expected JSON string.");
        for (;;) {
            const char = source[index++];
            if (char === undefined || char === "\n")
                throw new Error("Unterminated JSON string.");
            if (char === "\\") {
                index += 1;
                continue;
            }
            if (char === '"')
                return JSON.parse(source.slice(begin, index));
        }
    };
    const parseValue = (depth = 0) => {
        if (depth > 32)
            throw new Error("JSON frame nesting exceeds limit.");
        skipSpace();
        if (source[index] === '"')
            return parseString();
        if (source[index] === "[") {
            index += 1;
            const result = [];
            skipSpace();
            if (source[index] === "]") {
                index += 1;
                return result;
            }
            for (;;) {
                result.push(parseValue(depth + 1));
                skipSpace();
                const separator = source[index++];
                if (separator === "]")
                    return result;
                if (separator !== ",")
                    throw new Error("Expected JSON array separator.");
            }
        }
        if (source[index] === "{") {
            index += 1;
            const result = Object.create(null);
            const seen = new Set();
            skipSpace();
            if (source[index] === "}") {
                index += 1;
                return result;
            }
            for (;;) {
                skipSpace();
                const key = parseString();
                if (seen.has(key))
                    throw new Error("Duplicate JSON field.");
                seen.add(key);
                skipSpace();
                if (source[index++] !== ":")
                    throw new Error("Expected JSON colon.");
                result[key] = parseValue(depth + 1);
                skipSpace();
                const separator = source[index++];
                if (separator === "}")
                    return result;
                if (separator !== ",")
                    throw new Error("Expected JSON separator.");
            }
        }
        for (const [literal, value] of [["true", true], ["false", false], ["null", null]]) {
            if (source.startsWith(literal, index)) {
                index += literal.length;
                return value;
            }
        }
        const match = /^(?:0|[1-9][0-9]*)/u.exec(source.slice(index));
        if (match === null)
            throw new Error("Non-canonical JSON value.");
        index += match[0].length;
        const number = Number(match[0]);
        if (!Number.isSafeInteger(number))
            throw new Error("JSON integer exceeds safe range.");
        return number;
    };
    const result = parseValue();
    skipSpace();
    if (index !== source.length)
        throw new Error("Trailing JSON content.");
    return result;
}
function boundedMs(value, fallback) {
    const result = value ?? fallback;
    if (!Number.isSafeInteger(result) || result <= 0 || result > MAX_HOLD_MS) {
        throw new Error("Managed cut timeout must be an integer from 1 through 30000 milliseconds.");
    }
    return result;
}
function assertNotCancelled(signal) {
    if (signal?.aborted)
        throw new Error("Managed cut cancelled.");
}
async function acquireCutLease(gate, waitMs, maxHoldMs, signal) {
    assertNotCancelled(signal);
    if (signal === undefined)
        return await gate.acquireExclusive(waitMs, maxHoldMs);
    const deadline = performance.now() + waitMs;
    for (;;) {
        assertNotCancelled(signal);
        const remaining = Math.ceil(deadline - performance.now());
        if (remaining <= 0)
            throw new Error("Profile gate wait deadline exceeded.");
        let lease;
        try {
            lease = await gate.acquireExclusive(Math.min(remaining, GATE_CANCEL_POLL_MS), maxHoldMs);
        }
        catch (error) {
            if (!(error instanceof Error) || error.message !== "Profile gate wait deadline exceeded.") {
                throw error;
            }
            continue;
        }
        if (signal.aborted || performance.now() > deadline) {
            lease.close();
            assertNotCancelled(signal);
            throw new Error("Profile gate wait deadline exceeded.");
        }
        return lease;
    }
}
function normalizedLimits(input) {
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
        throw new Error("Managed cut limits are required.");
    }
    const values = {
        max_core_db_bytes: input.maxCoreDbBytes,
        max_stage_bytes: input.maxStageBytes,
        min_free_bytes: input.minFreeBytes,
        backup_pages_per_step: input.backupPagesPerStep,
    };
    if (Object.values(values).some((value) => !Number.isSafeInteger(value) || value <= 0) ||
        values.max_stage_bytes < values.max_core_db_bytes || values.backup_pages_per_step > 1024) {
        throw new Error("Managed cut limits are invalid.");
    }
    return values;
}
function checkedDirectory(fd, path) {
    const opened = fstatSync(fd, { bigint: true });
    const named = lstatSync(path, { bigint: true });
    if (!opened.isDirectory() || named.isSymbolicLink() ||
        opened.dev !== named.dev || opened.ino !== named.ino ||
        opened.uid !== BigInt(process.getuid()) || (opened.mode & 511n) !== 448n) {
        throw new Error("Managed cut directory is unsafe or changed.");
    }
    rejectAclGrants(fd);
}
function checkedRegistration(fd, path, profileId, profileRoot) {
    const opened = fstatSync(fd, { bigint: true });
    const named = lstatSync(path, { bigint: true });
    if (!opened.isFile() || named.isSymbolicLink() || opened.dev !== named.dev ||
        opened.ino !== named.ino || opened.uid !== BigInt(process.getuid()) ||
        (opened.mode & 511n) !== 384n || opened.nlink !== 1n || opened.size <= 0n ||
        opened.size > 65536n) {
        throw new Error("Managed cut registration is unsafe.");
    }
    rejectAclGrants(fd);
    const bytes = readFileSync(fd);
    const parsed = JSON.parse(bytes.toString("utf8"));
    const entry = exactObject(parsed, ["version", "profile_id", "runtime_root", "workspace_root",
        "staging_database", "main_device", "main_inode", "migration_contract_sha256", "instance_id"]);
    if (entry.version !== 1 || entry.profile_id !== profileId ||
        entry.runtime_root !== join(profileRoot, "runtime") ||
        entry.workspace_root !== join(profileRoot, "workspace") ||
        entry.staging_database !== join(profileRoot, "workspace", "database", "staging.sqlite") ||
        typeof entry.instance_id !== "string" || !HEX32.test(entry.instance_id) ||
        typeof entry.migration_contract_sha256 !== "string" ||
        !HASH.test(entry.migration_contract_sha256)) {
        throw new Error("Managed cut registration does not match profile.");
    }
    const after = fstatSync(fd, { bigint: true });
    if (after.dev !== opened.dev || after.ino !== opened.ino || after.size !== opened.size ||
        after.ctimeNs !== opened.ctimeNs || after.mtimeNs !== opened.mtimeNs) {
        throw new Error("Managed cut registration changed during inspection.");
    }
    return { digest: createHash("sha256").update(bytes).digest("hex"),
        schema: entry.migration_contract_sha256 };
}
function decimalIdentity(value) {
    if (typeof value !== "string" || !DECIMAL.test(value)) {
        throw new Error("Managed cut identity must be a canonical decimal string.");
    }
    const parsed = BigInt(value);
    if (parsed > 0xffffffffffffffffn)
        throw new Error("Managed cut identity exceeds native range.");
    return parsed;
}
function checkCutAlive(context, options, deadline) {
    context.lease.assertValid();
    if (performance.now() >= deadline || options.signal?.aborted) {
        throw new Error("Managed cut deadline expired or was cancelled.");
    }
}
function boundedPositive(value, maximum, label, allowZero = false) {
    if (!Number.isSafeInteger(value) || value < (allowZero ? 0 : 1) ||
        value > maximum)
        throw new Error(`Managed bundle ${label} is invalid.`);
    return value;
}
function treeRecord(fd, path, kind, mode, device, stagePath) {
    const status = fstatSync(fd, { bigint: true });
    const named = lstatSync(join(stagePath, path), { bigint: true });
    if ((kind === "dir" ? !status.isDirectory() : !status.isFile()) ||
        status.dev !== device || status.uid !== BigInt(process.getuid()) ||
        (status.mode & 511n) !== mode ||
        (kind === "file" && status.nlink !== 1n) || named.isSymbolicLink() ||
        named.dev !== status.dev || named.ino !== status.ino ||
        named.mtimeNs !== status.mtimeNs || named.ctimeNs !== status.ctimeNs) {
        throw new Error("Managed bundle tree member is unsafe.");
    }
    rejectAclGrants(fd);
    if ([status.uid, status.nlink, status.size].some((value) => value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER))) {
        throw new Error("Managed bundle identity exceeds safe range.");
    }
    return {
        path, kind, dev: status.dev.toString(), ino: status.ino.toString(),
        uid: Number(status.uid), mode: Number(status.mode & 511n),
        nlink: Number(status.nlink), size: Number(status.size),
        mtime_ns: status.mtimeNs.toString(), ctime_ns: status.ctimeNs.toString(),
    };
}
function asciiJsonString(value) {
    return JSON.stringify(value).replace(/[^\x00-\x7f]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
function canonicalJson(value) {
    if (Array.isArray(value))
        return `[${value.map(canonicalJson).join(",")}]`;
    if (value !== null && typeof value === "object") {
        return `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
            .map(([key, item]) => `${asciiJsonString(key)}:${canonicalJson(item)}`).join(",")}}`;
    }
    return typeof value === "string" ? asciiJsonString(value) : JSON.stringify(value);
}
function readChunk(fd, buffer) {
    return new Promise((resolvePromise, reject) => {
        read(fd, buffer, 0, buffer.length, null, (error, count) => {
            if (error)
                reject(error);
            else
                resolvePromise(count);
        });
    });
}
/** Re-enumerate fixed stage roles only after the reader's actual close/reap. */
async function finalBundleTree(context, options, deadline, maxStageBytes, maxManifestBytes, maxMembers, expected, common) {
    checkCutAlive(context, options, deadline);
    const rootStatus = fstatSync(context.stageFd, { bigint: true });
    const device = rootStatus.dev;
    const records = [treeRecord(context.stageFd, ".", "dir", 448n, device, context.stagePath)];
    const top = listAt(context.stageFd).sort();
    if (top.join(",") !== "attachments,db,manifest.json") {
        throw new Error("Managed bundle root layout changed.");
    }
    let dataMemberCount = 0;
    let dataMemberBytes = 0;
    const actualMembers = [];
    const addFile = async (parentFd, name, path, mode, limit, role) => {
        checkCutAlive(context, options, deadline);
        const fd = openFileAt(parentFd, name, constants.O_RDONLY | constants.O_NONBLOCK);
        try {
            const record = treeRecord(fd, path, "file", mode, device, context.stagePath);
            if (record.size <= 0 || record.size > limit)
                throw new Error("Managed bundle member exceeds limit.");
            const hash = createHash("sha256");
            const payload = role === "manifest" ? Buffer.allocUnsafe(record.size) : undefined;
            const chunk = Buffer.allocUnsafe(64 * 1024);
            let bytes = 0;
            for (;;) {
                checkCutAlive(context, options, deadline);
                const count = await readChunk(fd, chunk);
                if (count === 0)
                    break;
                if (bytes + count > limit || bytes + count > record.size) {
                    throw new Error("Managed bundle member exceeds limit.");
                }
                hash.update(chunk.subarray(0, count));
                if (payload !== undefined)
                    chunk.copy(payload, bytes, 0, count);
                bytes += count;
            }
            const after = treeRecord(fd, path, "file", mode, device, context.stagePath);
            if (canonicalJson(record) !== canonicalJson(after) || bytes !== record.size) {
                throw new Error("Managed bundle member identity changed.");
            }
            records.push(after);
            const sha256 = hash.digest("hex");
            if (role !== "manifest")
                actualMembers.push({ path, role, bytes, sha256 });
            return { bytes, sha256, payload };
        }
        finally {
            closeSync(fd);
        }
    };
    const dbFd = openExistingDirectoryAt(context.stageFd, "db");
    try {
        records.push(treeRecord(dbFd, "db", "dir", 448n, device, context.stagePath));
        if (listAt(dbFd).join(",") !== "core.sqlite") {
            throw new Error("Managed bundle database directory changed.");
        }
        dataMemberBytes += (await addFile(dbFd, "core.sqlite", "db/core.sqlite", 384n, 64 * 1024 * 1024, "database")).bytes;
        dataMemberCount += 1;
    }
    finally {
        closeSync(dbFd);
    }
    const attachmentsFd = openExistingDirectoryAt(context.stageFd, "attachments");
    try {
        records.push(treeRecord(attachmentsFd, "attachments", "dir", 448n, device, context.stagePath));
        const shards = listAt(attachmentsFd).sort();
        if (shards.length > 256)
            throw new Error("Managed bundle shard count exceeds limit.");
        for (const shard of shards) {
            checkCutAlive(context, options, deadline);
            if (!SHARD.test(shard) || dataMemberCount > maxMembers) {
                throw new Error("Managed bundle attachment layout is invalid.");
            }
            const shardFd = openExistingDirectoryAt(attachmentsFd, shard);
            try {
                records.push(treeRecord(shardFd, `attachments/${shard}`, "dir", 448n, device, context.stagePath));
                const members = listAt(shardFd).sort();
                if (members.length === 0 || members.length > maxMembers ||
                    dataMemberCount + members.length > maxMembers + 1) {
                    throw new Error("Managed bundle attachment shard count is invalid.");
                }
                for (const member of members) {
                    checkCutAlive(context, options, deadline);
                    if (!ATTACHMENT_MEMBER.test(member) || !member.startsWith(shard) ||
                        ++dataMemberCount > maxMembers + 1) {
                        throw new Error("Managed bundle attachment member is invalid.");
                    }
                    dataMemberBytes += (await addFile(shardFd, member, `attachments/${shard}/${member}`, 256n, 20 * 1024 * 1024, "attachment")).bytes;
                    if (dataMemberBytes > maxStageBytes)
                        throw new Error("Managed bundle stage exceeds limit.");
                }
            }
            finally {
                closeSync(shardFd);
            }
        }
    }
    finally {
        closeSync(attachmentsFd);
    }
    const manifest = await addFile(context.stageFd, "manifest.json", "manifest.json", 384n, maxManifestBytes, "manifest");
    const manifestSha256 = manifest.sha256;
    const manifestByteLength = manifest.bytes;
    if (manifest.payload === undefined)
        throw new Error("Managed bundle manifest is missing.");
    const parsed = strictBundleFrame(manifest.payload);
    const document = exactObject(parsed, ["format", "scope", "reference_registry_version",
        "limits_version", "limits_sha256", "cut_id", "core_version", "core_api_contract_version",
        "core_artifact_sha256", "migration_contract_sha256", "snapshot_point",
        "package_completed_at", "database", "members", "reference_count", "reference_sha256",
        "member_count", "member_bytes"]);
    const database = exactObject(document.database, ["path", "sha256", "bytes", "page_count",
        "application_id", "user_version", "schema_fingerprint", "migration_ledger_sha256",
        "migration_ledger_count", "migration_latest_id"]);
    const point = exactObject(document.snapshot_point, ["kind", "cut_id", "recorded_at",
        "db_sha256"]);
    if (canonicalJson(parsed) !== manifest.payload.toString("utf8") ||
        !Array.isArray(document.members) ||
        canonicalJson(document.members) !== canonicalJson(actualMembers.sort((a, b) => String(a.path) < String(b.path) ? -1 : String(a.path) > String(b.path) ? 1 : 0))) {
        throw new Error("Managed bundle manifest does not match actual members.");
    }
    if (document.format !== "core-committed-snapshot-manifest-v1" ||
        document.scope !== BUNDLE_SCOPE || document.reference_registry_version !== REGISTRY_VERSION ||
        document.limits_version !== BUNDLE_LIMITS_VERSION ||
        document.limits_sha256 !== common.limits_sha256 || document.cut_id !== common.cut_id ||
        document.core_version !== options.config.agentProfileV2.coreVersion ||
        document.core_api_contract_version !== options.config.agentProfileV2.coreApiContractVersion ||
        document.core_artifact_sha256 !== common.artifact_sha256 ||
        document.migration_contract_sha256 !== common.schema_sha256 ||
        document.package_completed_at !== expected.package_completed_at ||
        document.reference_count !== expected.reference_count ||
        document.reference_sha256 !== expected.reference_sha256 ||
        document.member_count !== dataMemberCount || document.member_bytes !== dataMemberBytes ||
        database.path !== "db/core.sqlite" || database.sha256 !== expected.db_sha256 ||
        database.bytes !== expected.db_bytes || database.page_count !== expected.db_page_count ||
        database.schema_fingerprint !== expected.schema_fingerprint ||
        database.migration_ledger_sha256 !== expected.migration_ledger_sha256 ||
        database.migration_ledger_count !== expected.migration_ledger_count ||
        point.kind !== "exclusive_core_commit_state" || point.cut_id !== common.cut_id ||
        point.recorded_at !== expected.snapshot_recorded_at || point.db_sha256 !== expected.db_sha256) {
        throw new Error("Managed bundle manifest binding differs.");
    }
    if (dataMemberBytes + manifestByteLength > maxStageBytes) {
        throw new Error("Managed bundle stage exceeds limit.");
    }
    checkCutAlive(context, options, deadline);
    // A second enumeration catches a same-name insertion after the first walk.
    if (listAt(context.stageFd).sort().join(",") !== top.join(",")) {
        throw new Error("Managed bundle root changed during final verification.");
    }
    records.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
    return {
        treeSha256: createHash("sha256").update(canonicalJson(records)).digest("hex"),
        manifestSha256, manifestByteLength, dataMemberCount, dataMemberBytes,
    };
}
function sendFrame(control, value) {
    const frame = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
    if (frame.length > MAX_FRAME || control.destroyed)
        throw new Error("Managed cut control is closed.");
    control.write(frame);
}
function commonReceipt(frame, operation, cutId, workerId, profileId, artifact, schema) {
    if (frame.version !== VERSION || frame.cut_id !== cutId || frame.worker_id !== workerId ||
        frame.profile_id !== profileId || frame.operation !== operation ||
        frame.artifact_sha256 !== artifact || frame.schema_sha256 !== schema ||
        frame.output_role !== "core.sqlite") {
        throw new Error("Managed cut receipt binding is invalid.");
    }
}
function stagedReceipt(frame, cutId, workerId, profileId, artifact, schema) {
    const staged = exactObject(frame, ["version", "type", "cut_id", "worker_id", "profile_id",
        "operation", "artifact_sha256", "schema_sha256", "output_role", "byte_length", "sha256",
        "page_count", "stage_dev", "stage_ino", "output_dev", "output_ino", "backup_complete", "source_closed"]);
    commonReceipt(staged, "core_snapshot", cutId, workerId, profileId, artifact, schema);
    if (staged.type !== "staged" || staged.backup_complete !== true || staged.source_closed !== true ||
        !Number.isSafeInteger(staged.byte_length) || staged.byte_length <= 0 ||
        !Number.isSafeInteger(staged.page_count) || staged.page_count <= 0 ||
        typeof staged.sha256 !== "string" || !HASH.test(staged.sha256)) {
        throw new Error("Managed cut staged receipt is invalid.");
    }
    for (const key of ["stage_dev", "stage_ino", "output_dev", "output_ino"])
        decimalIdentity(staged[key]);
    return staged;
}
function verifiedReceipt(frame, cutId, workerId, profileId, artifact, schema, staged) {
    const verified = exactObject(frame, ["version", "type", "cut_id", "worker_id", "profile_id",
        "operation", "artifact_sha256", "schema_sha256", "output_role", "byte_length", "sha256",
        "page_count", "schema_object_count", "journal_mode", "reader_closed"]);
    commonReceipt(verified, "core_readback", cutId, workerId, profileId, artifact, schema);
    if (verified.type !== "verified" || verified.reader_closed !== true ||
        verified.journal_mode !== "delete" || verified.byte_length !== staged.byte_length ||
        verified.sha256 !== staged.sha256 || verified.page_count !== staged.page_count ||
        !Number.isSafeInteger(verified.schema_object_count) || verified.schema_object_count < 0) {
        throw new Error("Managed cut readback receipt is invalid.");
    }
    return verified;
}
function bundleTerminalCommon(frame, type, operation, cutId, workerId, options, common) {
    if (frame.version !== BUNDLE_VERSION || frame.type !== type || frame.cut_id !== cutId ||
        frame.worker_id !== workerId || frame.profile_id !== options.profileId ||
        frame.operation !== operation || frame.artifact_sha256 !== common.artifact_sha256 ||
        frame.schema_sha256 !== common.schema_sha256 || frame.scope !== BUNDLE_SCOPE ||
        frame.registry_version !== REGISTRY_VERSION ||
        frame.limits_version !== BUNDLE_LIMITS_VERSION ||
        frame.core_version !== options.config.agentProfileV2.coreVersion ||
        frame.core_api_contract_version !== options.config.agentProfileV2.coreApiContractVersion) {
        throw new Error("Managed bundle receipt binding is invalid.");
    }
}
function bundleEvidence(frame, limits) {
    for (const key of ["bundle_stage_dev", "bundle_stage_ino", "db_stage_dev", "db_stage_ino",
        "db_output_dev", "db_output_ino"])
        decimalIdentity(frame[key]);
    boundedPositive(frame.db_bytes, limits.max_core_db_bytes, "database size");
    boundedPositive(frame.db_page_count, limits.max_core_db_bytes, "database page count");
    boundedPositive(frame.manifest_bytes, limits.max_manifest_bytes, "manifest size");
    boundedPositive(frame.member_count, limits.max_attachment_members + 1, "member count");
    boundedPositive(frame.member_bytes, limits.max_stage_bytes, "member bytes");
    boundedPositive(frame.reference_count, limits.max_references, "reference count", true);
    boundedPositive(frame.migration_ledger_count, 1_000_000, "migration ledger count");
    for (const key of ["db_sha256", "manifest_sha256", "reference_sha256",
        "schema_fingerprint", "migration_ledger_sha256"]) {
        if (typeof frame[key] !== "string" || !HASH.test(frame[key])) {
            throw new Error(`Managed bundle ${key} is invalid.`);
        }
    }
    for (const key of ["snapshot_recorded_at", "package_completed_at"]) {
        if (typeof frame[key] !== "string" || !UTC_INSTANT.test(frame[key])) {
            throw new Error(`Managed bundle ${key} is invalid.`);
        }
    }
    if (frame.member_bytes < frame.db_bytes ||
        frame.member_bytes + frame.manifest_bytes > limits.max_stage_bytes ||
        frame.package_completed_at < frame.snapshot_recorded_at) {
        throw new Error("Managed bundle evidence is inconsistent.");
    }
}
function bundleStagedReceipt(frame, cutId, workerId, options, common) {
    const staged = exactObject(frame, [...BUNDLE_TERMINAL_COMMON, ...BUNDLE_STAGED_FIELDS,
        "source_closed", "backup_complete"]);
    bundleTerminalCommon(staged, "bundle_staged", "core_bundle_snapshot", cutId, workerId, options, common);
    if (staged.source_closed !== true || staged.backup_complete !== true) {
        throw new Error("Managed bundle source or database backup did not close.");
    }
    bundleEvidence(staged, BUNDLE_LIMITS);
    return staged;
}
function bundleVerifiedReceipt(frame, staged, cutId, workerId, options, common) {
    const verified = exactObject(frame, [...BUNDLE_TERMINAL_COMMON, ...BUNDLE_STAGED_FIELDS,
        "schema_object_count", "journal_mode", "tree_identity_sha256", "reader_closed"]);
    bundleTerminalCommon(verified, "bundle_readback_verified", "core_bundle_readback", cutId, workerId, options, common);
    bundleEvidence(verified, BUNDLE_LIMITS);
    if (verified.reader_closed !== true || verified.journal_mode !== "delete" ||
        typeof verified.tree_identity_sha256 !== "string" ||
        !HASH.test(verified.tree_identity_sha256) ||
        !Number.isSafeInteger(verified.schema_object_count) ||
        verified.schema_object_count < 0) {
        throw new Error("Managed bundle reader receipt is invalid.");
    }
    for (const key of BUNDLE_STAGED_FIELDS) {
        if (verified[key] !== staged[key])
            throw new Error(`Managed bundle ${key} readback differs.`);
    }
    return verified;
}
async function runFixedChild(context, options, operation, request, deadline) {
    context.lease.assertValid();
    if (performance.now() >= deadline || options.signal?.aborted) {
        throw new Error("Managed cut deadline expired or was cancelled.");
    }
    await revalidatePythonExecutableForSpawn(options.config);
    context.lease.assertValid();
    if (performance.now() >= deadline || options.signal?.aborted) {
        throw new Error("Managed cut deadline expired or was cancelled.");
    }
    context.lease.reserveChild();
    const module = operation === "core_snapshot" || operation === "core_bundle_snapshot"
        ? "managed_cut_worker" : "managed_snapshot_reader";
    const protocolVersion = operation === "core_snapshot" || operation === "core_readback"
        ? VERSION : BUNDLE_VERSION;
    let child;
    try {
        child = spawn(options.config.pythonExecutable, ["-I", "-B", "-c", BOOTSTRAP, options.config.coreDistributionRoot, module], {
            cwd: options.config.coreDistributionRoot, shell: false, detached: false,
            env: {
                ...profileLocatorEnvironment(options),
                FINANCE_CUT_PROFILE_ID: options.profileId,
                FINANCE_CUT_STAGE_PATH: context.stagePath,
                FINANCE_CUT_ARTIFACT_SHA256: options.config.agentProfileV2.coreManifestSha256,
                LANG: "C.UTF-8", LC_ALL: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1",
                PYTHONNOUSERSITE: "1", PYTHONUTF8: "1",
            },
            stdio: ["ignore", "ignore", "ignore", "pipe", context.lease.fdForChild(),
                context.profileFd, context.stageFd],
        });
    }
    catch (error) {
        context.lease.unbindChild();
        throw error;
    }
    const control = child.stdio[3];
    return await new Promise((resolvePromise, reject) => {
        let bytes = Buffer.alloc(0);
        let ready = false;
        let terminal;
        let failure;
        let settled = false;
        let termTimer;
        let killTimer;
        const cleanup = () => {
            clearTimeout(deadlineTimer);
            if (termTimer !== undefined)
                clearTimeout(termTimer);
            if (killTimer !== undefined)
                clearTimeout(killTimer);
            options.signal?.removeEventListener("abort", onAbort);
        };
        const terminate = (reason) => {
            if (failure !== undefined || settled)
                return;
            failure = reason;
            child.kill("SIGTERM");
            termTimer = setTimeout(() => {
                if (settled)
                    return;
                child.kill("SIGKILL");
                killTimer = setTimeout(() => {
                    if (settled)
                        return;
                    settled = true;
                    unhealthy = true;
                    context.uncertain = true;
                    uncertainCuts.add(context);
                    cleanup();
                    reject(new Error("Managed cut child close/reap is unknown; profile remains held."));
                }, REAP_GRACE_MS);
            }, REAP_GRACE_MS);
        };
        const onAbort = () => terminate(new Error("Managed cut cancelled."));
        const deadlineTimer = setTimeout(() => terminate(new Error("Managed cut deadline expired.")), Math.max(0, Math.floor(deadline - performance.now())));
        child.once("error", () => terminate(new Error("Managed cut child startup failed.")));
        control?.on("error", () => terminate(new Error("Managed cut control failed.")));
        control?.on("data", (chunk) => {
            if (failure !== undefined || settled)
                return;
            bytes = Buffer.concat([bytes, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
            if (bytes.length > MAX_FRAME * 2)
                return terminate(new Error("Managed cut frame exceeded limit."));
            for (;;) {
                const end = bytes.indexOf(10);
                if (end < 0)
                    break;
                const line = bytes.subarray(0, end);
                bytes = bytes.subarray(end + 1);
                if (line.length > MAX_FRAME)
                    return terminate(new Error("Managed cut frame exceeded limit."));
                let parsed;
                try {
                    parsed = protocolVersion === BUNDLE_VERSION
                        ? strictBundleFrame(line) : JSON.parse(line.toString("utf8"));
                }
                catch {
                    return terminate(new Error("Managed cut frame is malformed."));
                }
                if (!ready) {
                    try {
                        const response = exactObject(parsed, ["version", "type", "cut_id", "worker_id"]);
                        if (response.version !== protocolVersion || response.type !== "ready" ||
                            response.cut_id !== request.cut_id || response.worker_id !== request.worker_id) {
                            throw new Error("Managed cut handshake differs.");
                        }
                        ready = true;
                        sendFrame(control, { version: protocolVersion, type: "go", cut_id: request.cut_id,
                            worker_id: request.worker_id });
                    }
                    catch {
                        return terminate(new Error("Managed cut handshake is invalid."));
                    }
                }
                else if (terminal === undefined) {
                    try {
                        terminal = exactObject(parsed, Object.keys(parsed));
                    }
                    catch {
                        return terminate(new Error("Managed cut terminal frame is invalid."));
                    }
                }
                else {
                    return terminate(new Error("Managed cut emitted extra frames."));
                }
            }
        });
        child.once("close", (code, signal) => {
            // A success frame and an `exit` event are insufficient; only close has
            // observed process exit and all inherited streams closed.
            let releaseError;
            try {
                context.lease.unbindChild();
            }
            catch {
                unhealthy = true;
                context.uncertain = true;
                uncertainCuts.add(context);
                releaseError = new Error("Managed cut child reservation could not be released.");
            }
            if (settled) {
                // The bounded wait reported unknown, but an eventual actual close is
                // now observed. Keep the attempt failed and release exclusion safely.
                if (releaseError === undefined && context.uncertain) {
                    try {
                        releaseContext(context);
                        uncertainCuts.delete(context);
                        unhealthy = uncertainCuts.size !== 0;
                    }
                    catch {
                        unhealthy = true;
                    }
                }
                return;
            }
            settled = true;
            cleanup();
            if (releaseError !== undefined)
                return reject(releaseError);
            if (failure !== undefined)
                return reject(failure);
            if (signal !== null || code !== 0 || !ready || terminal === undefined || bytes.length !== 0) {
                return reject(new Error("Managed cut child ended without a valid terminal receipt."));
            }
            resolvePromise(terminal);
        });
        options.signal?.addEventListener("abort", onAbort, { once: true });
        // Abort can occur after the pre-spawn check but before this listener exists.
        if (options.signal?.aborted)
            onAbort();
        if (failure === undefined && control === null)
            terminate(new Error("Managed cut control pipe is missing."));
        else if (failure === undefined && control !== null) {
            try {
                sendFrame(control, request);
            }
            catch {
                terminate(new Error("Managed cut request could not be sent."));
            }
        }
    });
}
/** Both public entries use this one preflight, EX lease and uncertain-close path. */
async function withManagedCut(options, limitsForRun, version, work) {
    if (unhealthy)
        throw new Error("Managed cut coordinator is unhealthy after uncertain child closure.");
    options = Object.freeze({ ...options });
    const layout = checkProfileAncestors(options);
    const limits = limitsForRun();
    const waitMs = boundedMs(options.waitMs, 5_000);
    const maxHoldMs = boundedMs(options.maxHoldMs, 30_000);
    assertNotCancelled(options.signal);
    await revalidatePythonExecutableForSpawn(options.config);
    assertNotCancelled(options.signal);
    await verifyCoreDistributionV1(options.config);
    assertNotCancelled(options.signal);
    const profileRoot = layout.profileRoot;
    const profileFd = openDirectory(profileRoot);
    let workFd;
    let stageFd;
    let gate;
    let lease;
    let context;
    try {
        checkedDirectory(profileFd, profileRoot);
        workFd = openExistingDirectoryAt(profileFd, "work");
        checkedDirectory(workFd, join(profileRoot, "work"));
        const registrationPath = join(profileRoot, ".managed-staging.v1.json");
        const registrationFd = openFileAt(profileFd, ".managed-staging.v1.json", constants.O_RDONLY | constants.O_NONBLOCK);
        let registration;
        try {
            registration = checkedRegistration(registrationFd, registrationPath, options.profileId, profileRoot);
        }
        finally {
            closeSync(registrationFd);
        }
        gate = openProfileGate(profileRoot);
        lease = await acquireCutLease(gate, waitMs, maxHoldMs, options.signal);
        lease.assertValid();
        assertNotCancelled(options.signal);
        const cutId = randomBytes(16).toString("hex");
        const stageName = `core-cut-${cutId}`;
        stageFd = createDirectoryExclusiveAt(workFd, stageName);
        const stagePath = join(profileRoot, "work", stageName);
        checkedDirectory(stageFd, stagePath);
        fsyncSync(workFd);
        context = { gate, lease, profileFd, workFd, stageFd, stagePath,
            released: false, uncertain: false };
        const deadline = performance.now() + maxHoldMs;
        const common = {
            version, cut_id: cutId, profile_id: options.profileId,
            registration_sha256: registration.digest,
            artifact_sha256: options.config.agentProfileV2.coreManifestSha256,
            schema_sha256: registration.schema,
            limits_sha256: createHash("sha256").update(JSON.stringify(Object.fromEntries(Object.entries(limits).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)))).digest("hex"),
            limits,
        };
        return await work({ context, deadline, cutId, common });
    }
    finally {
        if (context !== undefined) {
            if (!context.uncertain)
                releaseContext(context);
        }
        else {
            if (stageFd !== undefined)
                closeSync(stageFd);
            if (lease !== undefined)
                lease.close();
            if (gate !== undefined)
                gate.close();
            if (workFd !== undefined)
                closeSync(workFd);
            closeSync(profileFd);
        }
    }
}
/** Component-only: one EX covers both fixed children and their actual reap. */
export async function runManagedCoreSnapshot(options) {
    return await withManagedCut(options, () => normalizedLimits(options.limits), VERSION, async ({ context, deadline, cutId, common, }) => {
        const { lease, stageFd, stagePath } = context;
        const workerId = randomBytes(16).toString("hex");
        const stagedFrame = await runFixedChild(context, options, "core_snapshot", {
            ...common, worker_id: workerId, operation: "core_snapshot",
            remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
        }, deadline);
        const staged = stagedReceipt(stagedFrame, cutId, workerId, options.profileId, common.artifact_sha256, common.schema_sha256);
        const stageStat = fstatSync(stageFd, { bigint: true });
        if (decimalIdentity(staged.stage_dev) !== stageStat.dev ||
            decimalIdentity(staged.stage_ino) !== stageStat.ino) {
            throw new Error("Managed cut stage identity changed.");
        }
        lease.assertValid();
        const readerId = randomBytes(16).toString("hex");
        const readerFrame = await runFixedChild(context, options, "core_readback", {
            ...common, worker_id: readerId, operation: "core_readback",
            remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
            staged: {
                byte_length: staged.byte_length, sha256: staged.sha256, page_count: staged.page_count,
                stage_dev: staged.stage_dev, stage_ino: staged.stage_ino,
                output_dev: staged.output_dev, output_ino: staged.output_ino,
            },
        }, deadline);
        const verified = verifiedReceipt(readerFrame, cutId, readerId, options.profileId, common.artifact_sha256, common.schema_sha256, staged);
        lease.assertValid();
        return Object.freeze({ cutId, stagePath, byteLength: verified.byte_length,
            sha256: verified.sha256, pageCount: verified.page_count,
            schemaObjectCount: verified.schema_object_count, journalMode: "delete" });
    });
}
/** Fixed Core committed snapshot bundle; EX ends only after final Node tree verification. */
export async function runManagedCoreSnapshotBundle(options) {
    return await withManagedCut(options, () => ({ ...BUNDLE_LIMITS }), BUNDLE_VERSION, async ({ context, deadline, cutId, common, }) => {
        const trusted = {
            ...common,
            scope: BUNDLE_SCOPE,
            registry_version: REGISTRY_VERSION,
            limits_version: BUNDLE_LIMITS_VERSION,
            core_version: options.config.agentProfileV2.coreVersion,
            core_api_contract_version: options.config.agentProfileV2.coreApiContractVersion,
        };
        const workerId = randomBytes(16).toString("hex");
        const stagedFrame = await runFixedChild(context, options, "core_bundle_snapshot", {
            ...trusted, worker_id: workerId, operation: "core_bundle_snapshot",
            remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
        }, deadline);
        const staged = bundleStagedReceipt(stagedFrame, cutId, workerId, options, common);
        const stageStat = fstatSync(context.stageFd, { bigint: true });
        if (decimalIdentity(staged.bundle_stage_dev) !== stageStat.dev ||
            decimalIdentity(staged.bundle_stage_ino) !== stageStat.ino) {
            throw new Error("Managed bundle stage identity changed.");
        }
        const dbFd = openExistingDirectoryAt(context.stageFd, "db");
        try {
            const dbStat = fstatSync(dbFd, { bigint: true });
            if (decimalIdentity(staged.db_stage_dev) !== dbStat.dev ||
                decimalIdentity(staged.db_stage_ino) !== dbStat.ino) {
                throw new Error("Managed bundle database directory identity changed.");
            }
            checkedDirectory(dbFd, join(context.stagePath, "db"));
        }
        finally {
            closeSync(dbFd);
        }
        checkCutAlive(context, options, deadline);
        const readerId = randomBytes(16).toString("hex");
        const readerStaged = Object.fromEntries(BUNDLE_STAGED_FIELDS.map((key) => [key, staged[key]]));
        const readerFrame = await runFixedChild(context, options, "core_bundle_readback", {
            ...trusted, worker_id: readerId, operation: "core_bundle_readback",
            remaining_ms: Math.max(1, Math.floor(deadline - performance.now())),
            staged: readerStaged,
        }, deadline);
        const verified = bundleVerifiedReceipt(readerFrame, staged, cutId, readerId, options, common);
        const finalTree = await finalBundleTree(context, options, deadline, BUNDLE_LIMITS.max_stage_bytes, BUNDLE_LIMITS.max_manifest_bytes, BUNDLE_LIMITS.max_attachment_members, verified, common);
        if (finalTree.treeSha256 !== verified.tree_identity_sha256 ||
            finalTree.manifestSha256 !== verified.manifest_sha256 ||
            finalTree.manifestByteLength !== verified.manifest_bytes ||
            finalTree.dataMemberCount !== verified.member_count ||
            finalTree.dataMemberBytes !== verified.member_bytes) {
            throw new Error("Managed bundle changed after independent readback.");
        }
        checkCutAlive(context, options, deadline);
        return Object.freeze({
            version: "core-snapshot-bundle-receipt-v1",
            scope: BUNDLE_SCOPE,
            status: "snapshot_verified",
            cutId,
            stagePath: context.stagePath,
            manifestSha256: verified.manifest_sha256,
            manifestByteLength: verified.manifest_bytes,
            dbSha256: verified.db_sha256,
            dbByteLength: verified.db_bytes,
            memberCount: verified.member_count,
            referenceCount: verified.reference_count,
            memberBytes: verified.member_bytes,
            referenceDigest: verified.reference_sha256,
            snapshotPoint: verified.snapshot_recorded_at,
            packageCompletedAt: verified.package_completed_at,
            verifiedAt: new Date().toISOString(),
        });
    });
}
