import { close, constants, fchmod, fstat, fsync, read, write } from "node:fs";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const packageRoot = dirname(sourceDirectory).endsWith(`${join("", "dist")}`)
    ? dirname(dirname(sourceDirectory))
    : dirname(sourceDirectory);
const bindingPath = resolve(packageRoot, "build/Release/finance_bridge_posix.node");
if (!existsSync(bindingPath)) {
    throw new Error("Finance bridge POSIX boundary is not built for the pinned Node runtime.");
}
const native = createRequire(import.meta.url)(bindingPath);
const closeAsync = promisify(close);
const fchmodAsync = promisify(fchmod);
const fstatAsync = promisify(fstat);
const fsyncAsync = promisify(fsync);
function readChunk(fd, buffer, offset, length) {
    return new Promise((resolvePromise, reject) => {
        read(fd, buffer, offset, length, null, (error, bytesRead) => {
            if (error)
                reject(error);
            else
                resolvePromise(bytesRead);
        });
    });
}
function writeChunk(fd, buffer, offset, length) {
    return new Promise((resolvePromise, reject) => {
        write(fd, buffer, offset, length, null, (error, bytesWritten) => {
            if (error)
                reject(error);
            else
                resolvePromise(bytesWritten);
        });
    });
}
const UINT32_MAX = 0xffff_ffff;
const UINT64_MAX = 0xffffffffffffffffn;
const INT64_MIN = -0x8000000000000000n;
const INT64_MAX = 0x7fffffffffffffffn;
const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
function requireFileDescriptor(value) {
    if (!Number.isSafeInteger(value) || value < 0 || value > 0x7fff_ffff) {
        throw new TypeError("Expected a valid non-negative integer directory descriptor.");
    }
}
function requireEntryBasename(value) {
    if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value, "utf8") > 255 ||
        value === "." || value === ".." || value.includes("/") || value.includes("\0")) {
        throw new TypeError("Expected one safe directory-entry basename.");
    }
}
function validatedEntryIdentity(value) {
    if (typeof value !== "object" || value === null || Array.isArray(value) ||
        (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)) {
        throw new TypeError("Expected a plain complete file identity object.");
    }
    const fields = ["dev", "ino", "uid", "mode", "size", "ctimeNs", "mtimeNs"];
    for (const field of fields) {
        if (!Object.prototype.hasOwnProperty.call(value, field)) {
            throw new TypeError(`File identity is missing ${field}.`);
        }
    }
    const { dev, ino, uid, mode, size, ctimeNs, mtimeNs } = value;
    if (typeof dev !== "bigint" || dev < 0n || dev > UINT64_MAX ||
        typeof ino !== "bigint" || ino < 0n || ino > UINT64_MAX ||
        !Number.isSafeInteger(uid) || uid < 0 || uid > UINT32_MAX ||
        !Number.isSafeInteger(mode) || mode < 0 || mode > UINT32_MAX ||
        !Number.isSafeInteger(size) || size < 0 || size > MAX_SAFE_INTEGER ||
        typeof ctimeNs !== "bigint" || ctimeNs < INT64_MIN || ctimeNs > INT64_MAX ||
        typeof mtimeNs !== "bigint" || mtimeNs < INT64_MIN || mtimeNs > INT64_MAX) {
        throw new TypeError("File identity fields have invalid types or ranges.");
    }
    return { dev, ino, uid, mode, size, ctimeNs, mtimeNs };
}
export async function descriptorIdentity(fd) {
    const status = await fstatAsync(fd, { bigint: true });
    const size = Number(status.size);
    if (!Number.isSafeInteger(size))
        throw new Error("Descriptor size is outside the safe range.");
    return {
        dev: status.dev,
        ino: status.ino,
        uid: Number(status.uid),
        mode: Number(status.mode),
        size,
        ctimeNs: status.ctimeNs,
        mtimeNs: status.mtimeNs,
        isDirectory: status.isDirectory(),
        isFile: status.isFile(),
    };
}
export function descriptorIdentitySync(fd) {
    const status = native.descriptorIdentitySync(fd);
    if (!Number.isSafeInteger(status.size)) {
        throw new Error("Descriptor size is outside the safe range.");
    }
    return status;
}
export function openDirectory(path) {
    return native.openDirectory(path);
}
export function openPrivateDirectoryAt(parentFd, name) {
    return native.openDirectoryAt(parentFd, name, 0o700);
}
export function openExistingDirectoryAt(parentFd, name) {
    return native.openExistingDirectoryAt(parentFd, name);
}
export function openFileAt(directoryFd, name, flags, mode = 0) {
    return native.openFileAt(directoryFd, name, flags, mode);
}
export async function closeDescriptor(fd) {
    await closeAsync(fd);
}
export async function chmodDescriptor(fd, mode) {
    await fchmodAsync(fd, mode);
}
export async function syncDescriptor(fd) {
    await fsyncAsync(fd);
}
export async function readDescriptor(fd, maximum, afterInitialIdentity = () => undefined) {
    const before = await descriptorIdentity(fd);
    if (!before.isFile || (before.mode & 0o777) !== 0o600 || before.size <= 0 || before.size > maximum) {
        throw new Error("Handoff entry is not a private bounded regular file.");
    }
    await afterInitialIdentity();
    const buffer = Buffer.allocUnsafe(before.size);
    let offset = 0;
    while (offset < buffer.length) {
        const count = await readChunk(fd, buffer, offset, buffer.length - offset);
        if (count === 0)
            break;
        offset += count;
    }
    const after = await descriptorIdentity(fd);
    if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size ||
        before.uid !== after.uid || before.mode !== after.mode || !after.isFile ||
        before.ctimeNs !== after.ctimeNs || before.mtimeNs !== after.mtimeNs ||
        offset !== before.size) {
        throw new Error("Handoff entry changed during read.");
    }
    return buffer;
}
export async function writeDescriptor(fd, bytes) {
    let offset = 0;
    while (offset < bytes.length) {
        const count = await writeChunk(fd, bytes, offset, bytes.length - offset);
        if (count <= 0)
            throw new Error("Handoff entry could not be written completely.");
        offset += count;
    }
}
export function renameNoReplaceAt(directoryFd, source, target) {
    native.renameNoReplaceAt(directoryFd, source, target);
}
/**
 * Remove a regular file only when its descriptor-relative identity still matches.
 * The caller must hold the handoff flock. A same-UID process that ignores it
 * can race between the native identity check and unlinkat.
 */
export function unlinkAtIfIdentity(directoryFd, name, expectedIdentity) {
    if (arguments.length !== 3)
        throw new TypeError("Expected exactly three unlink arguments.");
    requireFileDescriptor(directoryFd);
    requireEntryBasename(name);
    const identity = validatedEntryIdentity(expectedIdentity);
    const result = native.unlinkAtIfIdentity(directoryFd, name, identity);
    if (result !== undefined) {
        throw new Error("Native unlink boundary returned an invalid result.");
    }
}
export function listAt(directoryFd) {
    return native.listAt(directoryFd);
}
export function freeBytes(directoryFd) {
    return native.freeBytes(directoryFd);
}
export { constants };
