import { createHash } from "node:crypto";
import { basename, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { flock } from "fs-ext";
import { chmodDescriptor, closeDescriptor, constants, descriptorIdentity, descriptorIdentitySync, freeBytes as descriptorFreeBytes, listAt, openDirectory, openExistingDirectoryAt, openFileAt, openPrivateDirectoryAt, readDescriptor, renameNoReplaceAt, syncDescriptor, writeDescriptor, } from "./posix.js";
import { captureIdentities } from "./protocol.js";
export const HANDOFF_PENDING_RECORD = ".finance-bridge.record.pending";
export const HANDOFF_PENDING_PAYLOAD = ".finance-bridge.payload.pending";
const LOCK_BASENAME = ".finance-bridge.lock.v1";
const RECORD_SUFFIX = ".handoff.json";
const MAX_RECORDS = 32;
const MAX_PAYLOAD_BYTES = 320_000_000;
const MAX_TREE_BYTES = 321_000_000;
const FREE_SPACE_RESERVE = 16 * 1024 * 1024;
const DEFAULT_LOCK_TIMEOUT_MS = 30_000;
const PUBLIC_ID = /^raw_intake_bridge_[0-9a-f]{32}$/u;
async function verifyPublishedHandoff(directoryFd, published, media, canonicalKey) {
    const payload = await readRegularAt(directoryFd, published.handoffFilename, 10_000_000);
    if (payload.byteLength !== media.byteSize || sha256(payload) !== media.contentHash) {
        throw new Error("Published handoff payload changed before capture.");
    }
    const recordName = `${published.rawIntakePublicId}${RECORD_SUFFIX}`;
    const record = parseRecord(await readRegularAt(directoryFd, recordName, 4_096));
    if (record.schema_version !== "finance-bridge-handoff-v1" ||
        record.raw_intake_public_id !== published.rawIntakePublicId ||
        record.canonical_key_hash !== sha256(canonicalKey) ||
        record.content_hash !== media.contentHash || record.byte_size !== media.byteSize ||
        record.detected_mime_type !== media.detectedMimeType ||
        record.canonical_extension !== media.canonicalExtension ||
        record.payload_basename !== published.handoffFilename) {
        throw new Error("Published handoff record changed before capture.");
    }
}
function sameEntryIdentity(left, right) {
    return left.isFile && right.isFile && left.dev === right.dev && left.ino === right.ino &&
        left.uid === right.uid && left.mode === right.mode && left.size === right.size &&
        left.ctimeNs === right.ctimeNs && left.mtimeNs === right.mtimeNs;
}
async function entryIdentityAt(directoryFd, name) {
    const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
    try {
        return await descriptorIdentity(fd);
    }
    finally {
        await closeDescriptor(fd);
    }
}
async function entryIdentityAtPinned(directoryFd, name) {
    const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
    try {
        return descriptorIdentitySync(fd);
    }
    finally {
        await closeDescriptor(fd);
    }
}
async function requireLockIdentity(directoryFd, lockFd, expected) {
    const held = await descriptorIdentity(lockFd);
    const path = await entryIdentityAt(directoryFd, LOCK_BASENAME);
    if (!sameEntryIdentity(held, expected) || !sameEntryIdentity(path, expected)) {
        throw new Error("Handoff lock identity changed.");
    }
}
async function requireCallbackBoundary(directoryFd, published, payloadFd, expectedPayloadIdentity, expectedRecordIdentity, expectedNames) {
    const payload = await entryIdentityAt(directoryFd, published.handoffFilename);
    const openedPayload = await descriptorIdentity(payloadFd);
    const record = await entryIdentityAt(directoryFd, `${published.rawIntakePublicId}${RECORD_SUFFIX}`);
    if (!sameEntryIdentity(payload, expectedPayloadIdentity) ||
        !sameEntryIdentity(openedPayload, expectedPayloadIdentity) ||
        !sameEntryIdentity(record, expectedRecordIdentity)) {
        throw new Error("Published handoff inode identity changed during capture.");
    }
    const names = listAt(directoryFd).sort();
    if (names.length !== expectedNames.length || names.some((name, index) => name !== expectedNames[index])) {
        throw new Error("Handoff inventory changed during capture.");
    }
    const current = await inventory(directoryFd);
    if (current.incompleteRecords.size > 0) {
        throw new Error("Incomplete handoff residue appeared during capture.");
    }
}
function sha256(bytes) {
    return createHash("sha256").update(bytes).digest("hex");
}
function lockOperation(fileDescriptor, operation) {
    return new Promise((resolve, reject) => {
        flock(fileDescriptor, operation, (error) => error ? reject(error) : resolve());
    });
}
function tryExclusiveLock(fileDescriptor) {
    return new Promise((resolve, reject) => {
        flock(fileDescriptor, "exnb", (error) => {
            if (error === null || error === undefined)
                return resolve(true);
            const code = error.code;
            if (code === "EAGAIN" || code === "EWOULDBLOCK")
                return resolve(false);
            reject(error);
        });
    });
}
async function acquireExclusiveLock(fileDescriptor, timeoutMs) {
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > DEFAULT_LOCK_TIMEOUT_MS) {
        throw new Error("Handoff lock timeout is invalid.");
    }
    const deadline = performance.now() + timeoutMs;
    while (!await tryExclusiveLock(fileDescriptor)) {
        const remaining = deadline - performance.now();
        if (remaining <= 0)
            throw new Error("Handoff lock deadline exceeded.");
        await delay(Math.min(25, Math.ceil(remaining)));
    }
}
async function requireDirectoryPathIdentity(path, expected) {
    let pathFd;
    try {
        pathFd = openDirectory(path);
        const actual = await descriptorIdentity(pathFd);
        if (!actual.isDirectory || actual.dev !== expected.dev || actual.ino !== expected.ino ||
            (actual.mode & 0o077) !== 0) {
            throw new Error("Handoff directory identity changed.");
        }
    }
    finally {
        if (pathFd !== undefined)
            await closeDescriptor(pathFd);
    }
}
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function parseRecord(bytes) {
    if (bytes.byteLength > 4_096)
        throw new Error("Handoff record exceeds its bounded size.");
    let value;
    try {
        value = JSON.parse(bytes.toString("utf8"));
    }
    catch (error) {
        throw new Error("Handoff record is invalid JSON.", { cause: error });
    }
    if (!isRecord(value))
        throw new Error("Handoff record must be an object.");
    const exact = [
        "schema_version", "raw_intake_public_id", "canonical_key_hash", "content_hash",
        "byte_size", "detected_mime_type", "canonical_extension", "payload_basename",
    ];
    if (Object.keys(value).length !== exact.length || exact.some((field) => !(field in value)) ||
        value.schema_version !== "finance-bridge-handoff-v1" ||
        typeof value.raw_intake_public_id !== "string" || !PUBLIC_ID.test(value.raw_intake_public_id) ||
        typeof value.canonical_key_hash !== "string" || !/^[0-9a-f]{64}$/u.test(value.canonical_key_hash) ||
        typeof value.content_hash !== "string" || !/^[0-9a-f]{64}$/u.test(value.content_hash) ||
        !Number.isSafeInteger(value.byte_size) || value.byte_size <= 0 ||
        !["image/jpeg", "image/png"].includes(value.detected_mime_type) ||
        ![".jpg", ".png"].includes(value.canonical_extension) ||
        typeof value.payload_basename !== "string" ||
        value.payload_basename !== `${value.raw_intake_public_id}${value.canonical_extension}`) {
        throw new Error("Handoff record fields are invalid.");
    }
    return value;
}
function serializedRecord(record) {
    return Buffer.from(`${JSON.stringify(record)}\n`, "utf8");
}
async function readRegularAt(directoryFd, name, maximum) {
    const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
    try {
        return await readDescriptor(fd, maximum);
    }
    finally {
        await closeDescriptor(fd);
    }
}
async function publishPending(directoryFd, pendingName, finalName, bytes, afterFsync, afterPin, afterPublish, hook) {
    let pendingMatches = false;
    let pendingExists = false;
    try {
        const existing = await readRegularAt(directoryFd, pendingName, Math.max(bytes.byteLength, 4_096));
        pendingExists = true;
        pendingMatches = existing.equals(bytes);
    }
    catch (error) {
        if (error.code !== "ENOENT")
            throw error;
    }
    if (pendingExists && !pendingMatches) {
        throw new Error("Foreign handoff pending residue blocks intake.");
    }
    if (!pendingMatches) {
        const fd = openFileAt(directoryFd, pendingName, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
        try {
            await writeDescriptor(fd, bytes);
            await syncDescriptor(fd);
            await chmodDescriptor(fd, 0o600);
        }
        finally {
            await closeDescriptor(fd);
        }
    }
    await hook?.(afterFsync);
    const pendingBefore = await readRegularAt(directoryFd, pendingName, Math.max(bytes.byteLength, 4_096));
    if (!pendingBefore.equals(bytes))
        throw new Error("Handoff pending content changed before publication.");
    try {
        renameNoReplaceAt(directoryFd, pendingName, finalName);
    }
    catch (error) {
        if (error.code === "EEXIST") {
            throw new Error("Handoff final already exists while pending residue is present.", { cause: error });
        }
        throw error;
    }
    // Pin and snapshot the inode synchronously before any await. A directory
    // fsync is required for durability, but it must not become a window where a
    // same-UID replacement can establish a new callback baseline.
    const finalFd = openFileAt(directoryFd, finalName, constants.O_RDONLY);
    try {
        const publishedIdentity = descriptorIdentitySync(finalFd);
        await hook?.(afterPin);
        await syncDescriptor(directoryFd);
        await hook?.(afterPublish);
        try {
            await readRegularAt(directoryFd, pendingName, Math.max(bytes.byteLength, 4_096));
            throw new Error("Atomic handoff publication left an unexpected pending entry.");
        }
        catch (error) {
            if (error.code !== "ENOENT")
                throw error;
        }
        const final = await readRegularAt(directoryFd, finalName, Math.max(bytes.byteLength, 4_096));
        if (!final.equals(bytes)) {
            const subject = finalName.endsWith(RECORD_SUFFIX) ? "record" : "payload";
            throw new Error(`Published handoff ${subject} content changed after publication.`);
        }
        if (!sameEntryIdentity(await entryIdentityAt(directoryFd, finalName), publishedIdentity)) {
            throw new Error("Published handoff inode identity changed after publication.");
        }
        return publishedIdentity;
    }
    finally {
        await closeDescriptor(finalFd);
    }
}
async function requirePendingCompatible(directoryFd, pendingName, expected) {
    try {
        const pending = await readRegularAt(directoryFd, pendingName, Math.max(expected.byteLength, 4_096));
        if (!pending.equals(expected)) {
            throw new Error("Foreign handoff pending residue blocks intake.");
        }
        return true;
    }
    catch (error) {
        if (error.code !== "ENOENT")
            throw error;
        return false;
    }
}
async function defaultFreeBytes(directoryFd) {
    return descriptorFreeBytes(directoryFd);
}
async function inventory(directoryFd) {
    const entries = listAt(directoryFd);
    const records = new Map();
    let treeBytes = 0;
    let payloadBytes = 0;
    const payloadNames = new Set();
    for (const name of entries) {
        let fd;
        try {
            fd = openFileAt(directoryFd, name, constants.O_RDONLY);
        }
        catch (error) {
            if (error.code === "ELOOP") {
                throw new Error("Handoff tree contains a symlink.", { cause: error });
            }
            throw error;
        }
        let status;
        try {
            status = await descriptorIdentity(fd);
        }
        finally {
            await closeDescriptor(fd);
        }
        if (!status.isFile)
            throw new Error("Handoff tree contains an unknown non-file entry.");
        if ((status.mode & 0o077) !== 0)
            throw new Error("Handoff entry permissions are not private.");
        treeBytes += status.size;
        if (name === LOCK_BASENAME || name === HANDOFF_PENDING_RECORD || name === HANDOFF_PENDING_PAYLOAD) {
            continue;
        }
        if (name.endsWith(RECORD_SUFFIX)) {
            const id = name.slice(0, -RECORD_SUFFIX.length);
            if (!PUBLIC_ID.test(id))
                throw new Error("Handoff tree contains an unknown record.");
            const record = parseRecord(await readRegularAt(directoryFd, name, 4_096));
            if (record.raw_intake_public_id !== id)
                throw new Error("Handoff record filename mismatch.");
            records.set(id, record);
            continue;
        }
        if (/^raw_intake_bridge_[0-9a-f]{32}\.(?:jpg|png)$/u.test(name)) {
            payloadBytes += status.size;
            payloadNames.add(name);
            continue;
        }
        throw new Error("Handoff tree contains an unknown entry.");
    }
    const incompleteRecords = new Set();
    const referencedPayloads = new Set();
    for (const [id, record] of records) {
        referencedPayloads.add(record.payload_basename);
        if (!entries.includes(record.payload_basename)) {
            incompleteRecords.add(id);
            continue;
        }
        if (record.byte_size > 10_000_000) {
            throw new Error("Handoff record exceeds the receipt byte limit.");
        }
        const payload = await readRegularAt(directoryFd, record.payload_basename, 10_000_000);
        if (payload.byteLength !== record.byte_size || sha256(payload) !== record.content_hash) {
            throw new Error("Handoff payload does not match its slot record.");
        }
        const jpeg = payload.length >= 3 && payload[0] === 0xff &&
            payload[1] === 0xd8 && payload[2] === 0xff;
        const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
        const png = payload.length >= pngSignature.length &&
            payload.subarray(0, pngSignature.length).equals(pngSignature);
        if ((record.detected_mime_type === "image/jpeg" && (!jpeg || record.canonical_extension !== ".jpg")) ||
            (record.detected_mime_type === "image/png" && (!png || record.canonical_extension !== ".png"))) {
            throw new Error("Handoff payload magic does not match its slot record.");
        }
    }
    for (const payloadName of payloadNames) {
        if (!referencedPayloads.has(payloadName)) {
            throw new Error("Handoff tree contains an unknown orphan payload.");
        }
    }
    return { recordCount: records.size, payloadBytes, treeBytes, incompleteRecords };
}
export class HandoffPublisher {
    workspaceRoot;
    options;
    constructor(workspaceRoot, options = {}) {
        this.workspaceRoot = workspaceRoot;
        this.options = options;
    }
    async publish(canonicalKey, rawIntakePublicId, media) {
        return await this.withPublished(canonicalKey, rawIntakePublicId, media, async (published) => published);
    }
    async withRetained(canonicalKey, rawIntakePublicId, callback, lockTimeoutMs = this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS) {
        if (!PUBLIC_ID.test(rawIntakePublicId) ||
            captureIdentities(canonicalKey).rawIntakePublicId !== rawIntakePublicId) {
            throw new Error("Handoff replay identity is invalid.");
        }
        const handoffPath = join(this.workspaceRoot, "handoff");
        const workspaceFd = openDirectory(this.workspaceRoot);
        let directoryFd;
        try {
            try {
                directoryFd = openExistingDirectoryAt(workspaceFd, "handoff");
            }
            catch (error) {
                if (error.code === "ENOENT")
                    return undefined;
                throw error;
            }
        }
        finally {
            await closeDescriptor(workspaceFd);
        }
        if (directoryFd === undefined)
            return undefined;
        const directoryStatus = await descriptorIdentity(directoryFd);
        if (!directoryStatus.isDirectory || directoryStatus.uid !== process.getuid?.() ||
            (directoryStatus.mode & 0o077) !== 0) {
            await closeDescriptor(directoryFd);
            this.options.markUnhealthy?.();
            throw new Error("Handoff directory is not private.");
        }
        let lockFd;
        try {
            lockFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDWR | constants.O_NOFOLLOW);
            const lockIdentity = await descriptorIdentity(lockFd);
            if (!lockIdentity.isFile || lockIdentity.uid !== process.getuid?.() ||
                (lockIdentity.mode & 0o077) !== 0) {
                throw new Error("Handoff replay lock is not private.");
            }
            await acquireExclusiveLock(lockFd, lockTimeoutMs);
            try {
                await requireDirectoryPathIdentity(handoffPath, directoryStatus);
                const names = listAt(directoryFd);
                if (names.includes(HANDOFF_PENDING_RECORD) || names.includes(HANDOFF_PENDING_PAYLOAD)) {
                    throw new Error("Unresolved handoff pending residue blocks replay.");
                }
                const current = await inventory(directoryFd);
                if (current.incompleteRecords.size > 0) {
                    throw new Error("Incomplete handoff residue blocks replay.");
                }
                const recordName = `${rawIntakePublicId}${RECORD_SUFFIX}`;
                let record;
                try {
                    record = parseRecord(await readRegularAt(directoryFd, recordName, 4_096));
                }
                catch (error) {
                    if (error.code === "ENOENT")
                        return undefined;
                    throw error;
                }
                if (record.raw_intake_public_id !== rawIntakePublicId ||
                    record.canonical_key_hash !== sha256(canonicalKey)) {
                    throw new Error("Retained handoff identity does not match replay.");
                }
                const bytes = await readRegularAt(directoryFd, record.payload_basename, 10_000_000);
                const media = {
                    bytes,
                    byteSize: record.byte_size,
                    contentHash: record.content_hash,
                    detectedMimeType: record.detected_mime_type,
                    canonicalExtension: record.canonical_extension,
                };
                const published = {
                    handoffFilename: record.payload_basename,
                    recordPath: join(handoffPath, recordName),
                    payloadPath: join(handoffPath, record.payload_basename),
                    rawIntakePublicId,
                    contentHash: record.content_hash,
                };
                await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
                const payloadFd = openFileAt(directoryFd, record.payload_basename, constants.O_RDONLY);
                try {
                    const payloadIdentity = descriptorIdentitySync(payloadFd);
                    const recordIdentity = await entryIdentityAtPinned(directoryFd, recordName);
                    const expectedNames = listAt(directoryFd).sort();
                    if (!payloadIdentity.isFile || payloadIdentity.size !== record.byte_size ||
                        payloadIdentity.uid !== process.getuid?.() ||
                        (payloadIdentity.mode & 0o777) !== 0o600) {
                        throw new Error("Retained handoff descriptor is unsafe before replay.");
                    }
                    await this.options.hook?.("before-callback");
                    await requireLockIdentity(directoryFd, lockFd, lockIdentity);
                    await requireCallbackBoundary(directoryFd, published, payloadFd, payloadIdentity, recordIdentity, expectedNames);
                    const result = await callback(published, payloadFd, media);
                    await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
                    await requireLockIdentity(directoryFd, lockFd, lockIdentity);
                    await requireCallbackBoundary(directoryFd, published, payloadFd, payloadIdentity, recordIdentity, expectedNames);
                    await requireDirectoryPathIdentity(handoffPath, directoryStatus);
                    return result;
                }
                finally {
                    await closeDescriptor(payloadFd);
                }
            }
            finally {
                const after = await descriptorIdentity(lockFd);
                let pathIdentity;
                try {
                    const pathFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDONLY);
                    try {
                        pathIdentity = await descriptorIdentity(pathFd);
                    }
                    finally {
                        await closeDescriptor(pathFd);
                    }
                }
                catch {
                    pathIdentity = undefined;
                }
                if (after.dev !== lockIdentity.dev || after.ino !== lockIdentity.ino || !after.isFile) {
                    throw new Error("Handoff lock identity changed.");
                }
                if (pathIdentity === undefined || pathIdentity.dev !== lockIdentity.dev ||
                    pathIdentity.ino !== lockIdentity.ino || !pathIdentity.isFile) {
                    throw new Error("Handoff lock identity changed.");
                }
                await lockOperation(lockFd, "un");
            }
        }
        catch (error) {
            this.options.markUnhealthy?.();
            throw error;
        }
        finally {
            if (lockFd !== undefined)
                await closeDescriptor(lockFd);
            await closeDescriptor(directoryFd);
        }
    }
    async withPublished(canonicalKey, rawIntakePublicId, media, callback, lockTimeoutMs = this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS) {
        try {
            if (!PUBLIC_ID.test(rawIntakePublicId) || media.byteSize !== media.bytes.byteLength ||
                captureIdentities(canonicalKey).rawIntakePublicId !== rawIntakePublicId ||
                sha256(media.bytes) !== media.contentHash) {
                throw new Error("Handoff identity or content is invalid.");
            }
            const handoffPath = join(this.workspaceRoot, "handoff");
            const workspaceFd = openDirectory(this.workspaceRoot);
            let directoryFd;
            try {
                directoryFd = openPrivateDirectoryAt(workspaceFd, "handoff");
            }
            finally {
                await closeDescriptor(workspaceFd);
            }
            if (directoryFd === undefined)
                throw new Error("Handoff directory could not be opened.");
            const directoryStatus = await descriptorIdentity(directoryFd);
            if (!directoryStatus.isDirectory || directoryStatus.uid !== process.getuid?.() ||
                (directoryStatus.mode & 0o077) !== 0) {
                throw new Error("Handoff directory is not private.");
            }
            const lockFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDWR | constants.O_CREAT | constants.O_NOFOLLOW, 0o600);
            try {
                await chmodDescriptor(lockFd, 0o600);
                const lockIdentity = await descriptorIdentity(lockFd);
                if (!lockIdentity.isFile)
                    throw new Error("Handoff lock is not a regular file.");
                await acquireExclusiveLock(lockFd, lockTimeoutMs);
                await this.options.hook?.("after-lock");
                try {
                    await requireDirectoryPathIdentity(handoffPath, directoryStatus);
                    const slot = await this.publishLocked(directoryFd, handoffPath, canonicalKey, rawIntakePublicId, media);
                    const { published } = slot;
                    await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
                    await requireDirectoryPathIdentity(handoffPath, directoryStatus);
                    const payloadFd = openFileAt(directoryFd, published.handoffFilename, constants.O_RDONLY);
                    try {
                        const payloadIdentity = await descriptorIdentity(payloadFd);
                        const expectedNames = listAt(directoryFd).sort();
                        if (!payloadIdentity.isFile || payloadIdentity.size !== media.byteSize ||
                            payloadIdentity.uid !== process.getuid?.() ||
                            (payloadIdentity.mode & 0o777) !== 0o600) {
                            throw new Error("Published handoff descriptor is unsafe before capture.");
                        }
                        await this.options.hook?.("before-callback");
                        await requireLockIdentity(directoryFd, lockFd, lockIdentity);
                        await requireCallbackBoundary(directoryFd, published, payloadFd, slot.payloadIdentity, slot.recordIdentity, expectedNames);
                        const result = await callback(published, payloadFd);
                        await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
                        await requireLockIdentity(directoryFd, lockFd, lockIdentity);
                        await requireCallbackBoundary(directoryFd, published, payloadFd, slot.payloadIdentity, slot.recordIdentity, expectedNames);
                        await requireDirectoryPathIdentity(handoffPath, directoryStatus);
                        return result;
                    }
                    finally {
                        await closeDescriptor(payloadFd);
                    }
                }
                finally {
                    const after = await descriptorIdentity(lockFd);
                    let pathIdentity;
                    try {
                        const pathFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDONLY);
                        try {
                            pathIdentity = await descriptorIdentity(pathFd);
                        }
                        finally {
                            await closeDescriptor(pathFd);
                        }
                    }
                    catch {
                        pathIdentity = undefined;
                    }
                    if (after.dev !== lockIdentity.dev || after.ino !== lockIdentity.ino ||
                        pathIdentity === undefined || pathIdentity.dev !== lockIdentity.dev ||
                        pathIdentity.ino !== lockIdentity.ino || !pathIdentity.isFile) {
                        throw new Error("Handoff lock identity changed.");
                    }
                    await lockOperation(lockFd, "un");
                }
            }
            finally {
                await closeDescriptor(lockFd);
                await closeDescriptor(directoryFd);
            }
        }
        catch (error) {
            this.options.markUnhealthy?.();
            throw error;
        }
    }
    async publishLocked(directoryFd, directory, canonicalKey, rawIntakePublicId, media) {
        const payloadBasename = `${rawIntakePublicId}${media.canonicalExtension}`;
        const recordBasename = `${rawIntakePublicId}${RECORD_SUFFIX}`;
        const record = {
            schema_version: "finance-bridge-handoff-v1",
            raw_intake_public_id: rawIntakePublicId,
            canonical_key_hash: sha256(canonicalKey),
            content_hash: media.contentHash,
            byte_size: media.byteSize,
            detected_mime_type: media.detectedMimeType,
            canonical_extension: media.canonicalExtension,
            payload_basename: payloadBasename,
        };
        const recordBytes = serializedRecord(record);
        const recordPath = join(directory, recordBasename);
        const payloadPath = join(directory, payloadBasename);
        // Both fixed pending names are inspected before any mutation. Exact bytes
        // are resumable; foreign or unverifiable residue blocks the whole intake.
        const recordPending = await requirePendingCompatible(directoryFd, HANDOFF_PENDING_RECORD, recordBytes);
        const payloadPending = await requirePendingCompatible(directoryFd, HANDOFF_PENDING_PAYLOAD, media.bytes);
        let recordExists = false;
        try {
            const existing = parseRecord(await readRegularAt(directoryFd, recordBasename, 4_096));
            recordExists = true;
            if (JSON.stringify(existing) !== JSON.stringify(record)) {
                throw new Error("Handoff slot conflict for canonical message identity.");
            }
        }
        catch (error) {
            if (error.code !== "ENOENT")
                throw error;
        }
        if (payloadPending && !recordPending && !recordExists) {
            throw new Error("Orphan handoff payload pending residue blocks intake.");
        }
        let payloadExists = false;
        try {
            const existing = await readRegularAt(directoryFd, payloadBasename, 10_000_000);
            payloadExists = true;
            if (existing.byteLength !== media.byteSize || sha256(existing) !== media.contentHash) {
                throw new Error("Handoff payload conflict for canonical message identity.");
            }
        }
        catch (error) {
            if (error.code !== "ENOENT")
                throw error;
        }
        if ((recordExists && recordPending) || (payloadExists && payloadPending)) {
            throw new Error("Published handoff and pending residue coexist; intake is blocked.");
        }
        const current = await inventory(directoryFd);
        if (recordExists && payloadExists) {
            if (current.incompleteRecords.size > 0) {
                throw new Error("Foreign handoff residue blocks replay.");
            }
            return {
                published: {
                    handoffFilename: payloadBasename,
                    recordPath,
                    payloadPath,
                    rawIntakePublicId,
                    contentHash: media.contentHash,
                },
                recordIdentity: await entryIdentityAtPinned(directoryFd, recordBasename),
                payloadIdentity: await entryIdentityAtPinned(directoryFd, payloadBasename),
            };
        }
        for (const incomplete of current.incompleteRecords) {
            if (incomplete !== rawIntakePublicId)
                throw new Error("Foreign handoff residue blocks intake.");
        }
        if (!recordExists && current.recordCount >= MAX_RECORDS) {
            throw new Error("Handoff record quota of 32 is exhausted.");
        }
        const additionalPayloadBytes = payloadExists || payloadPending ? 0 : media.byteSize;
        const additionalRecord = recordExists || recordPending ? 0 : recordBytes.byteLength;
        if (current.payloadBytes + additionalPayloadBytes > MAX_PAYLOAD_BYTES) {
            throw new Error("Handoff payload quota is exhausted.");
        }
        if (current.treeBytes + additionalPayloadBytes + additionalRecord > MAX_TREE_BYTES) {
            throw new Error("Handoff tree quota is exhausted.");
        }
        const freeBytes = await (this.options.freeBytes ?? defaultFreeBytes)(directoryFd);
        const sourceCopyBytes = payloadExists || payloadPending ? media.byteSize : 2 * media.byteSize;
        if (freeBytes < sourceCopyBytes + FREE_SPACE_RESERVE) {
            throw new Error("Handoff filesystem free space is below the safety reserve.");
        }
        const recordIdentity = recordExists
            ? await entryIdentityAtPinned(directoryFd, recordBasename)
            : await publishPending(directoryFd, HANDOFF_PENDING_RECORD, recordBasename, recordBytes, "after-record-fsync", "after-record-pin", "after-record-publish", this.options.hook);
        const payloadIdentity = payloadExists
            ? await entryIdentityAtPinned(directoryFd, payloadBasename)
            : await publishPending(directoryFd, HANDOFF_PENDING_PAYLOAD, payloadBasename, media.bytes, "after-payload-fsync", "after-payload-pin", "after-payload-publish", this.options.hook);
        return {
            published: {
                handoffFilename: basename(payloadPath),
                recordPath,
                payloadPath,
                rawIntakePublicId,
                contentHash: media.contentHash,
            },
            recordIdentity,
            payloadIdentity,
        };
    }
}
