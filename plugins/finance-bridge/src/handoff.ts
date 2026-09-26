import { createHash } from "node:crypto";
import { basename, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { flock } from "fs-ext";

import type { ValidatedMedia } from "./media.js";
import {
  chmodDescriptor,
  closeDescriptor,
  constants,
  descriptorIdentity,
  descriptorIdentitySync,
  freeBytes as descriptorFreeBytes,
  listAt,
  openDirectory,
  openExistingDirectoryAt,
  openFileAt,
  openPrivateDirectoryAt,
  readDescriptor,
  renameNoReplaceAt,
  syncDescriptor,
  unlinkAtIfIdentity,
  writeDescriptor,
} from "./posix.js";
import type { DescriptorIdentity } from "./posix.js";
import { captureIdentities } from "./protocol.js";

export const HANDOFF_PENDING_RECORD = ".finance-bridge.record.pending";
export const HANDOFF_PENDING_PAYLOAD = ".finance-bridge.payload.pending";
const LOCK_BASENAME = ".finance-bridge.lock.v1";
const RECORD_SUFFIX = ".handoff.json";
const RECLAIM_SUFFIX = ".reclaim.json";
const MAX_RECORDS = 32;
const MAX_PAYLOAD_BYTES = 320_000_000;
const MAX_TREE_BYTES = 321_000_000;
const FREE_SPACE_RESERVE = 16 * 1024 * 1024;
const DEFAULT_LOCK_TIMEOUT_MS = 30_000;
const PUBLIC_ID = /^raw_intake_bridge_[0-9a-f]{32}$/u;

export type HandoffPhase =
  | "after-lock"
  | "after-record-fsync"
  | "after-record-pin"
  | "after-record-publish"
  | "after-payload-fsync"
  | "after-payload-pin"
  | "after-payload-publish"
  | "before-callback"
  | "after-reclaim-payload-unlink"
  | "after-reclaim-payload-fsync"
  | "after-reclaim-record-unlink"
  | "after-reclaim-record-fsync"
  | "after-reclaim-intent-unlink"
  | "after-reclaim-intent-fsync";
export type HandoffHook = (phase: HandoffPhase) => void | Promise<void>;

interface SlotRecord {
  schema_version: "finance-bridge-handoff-v1";
  raw_intake_public_id: string;
  canonical_key_hash: string;
  content_hash: string;
  byte_size: number;
  detected_mime_type: "image/jpeg" | "image/png";
  canonical_extension: ".jpg" | ".png";
  payload_basename: string;
}

/** A claim identifies one Core-owned original; it does not itself prove custody. */
export interface ReclaimClaim {
  rawIntakePublicId: string;
  jobPublicId: string;
  canonicalKeyHash: string;
  ingressIdentityDigest: string;
  attachmentContentHash: string;
}

interface SavedIdentity {
  dev: string;
  ino: string;
  uid: number;
  mode: number;
  size: number;
  ctimeNs: string;
  mtimeNs: string;
}

interface ReclaimIntent {
  schema_version: "finance-bridge-reclaim-v1";
  claim: ReclaimClaim;
  record_basename: string;
  payload_basename: string;
  record_sha256: string;
  payload_sha256: string;
  record_identity: SavedIdentity;
  payload_identity: SavedIdentity;
  directory_identity: SavedIdentity;
}

interface DirectoryInventory {
  recordCount: number;
  payloadBytes: number;
  treeBytes: number;
  incompleteRecords: Set<string>;
}

export interface PublishedHandoff {
  handoffFilename: string;
  recordPath: string;
  payloadPath: string;
  rawIntakePublicId: string;
  contentHash: string;
}

interface PublishedSlot {
  published: PublishedHandoff;
  payloadIdentity: DescriptorIdentity;
  recordIdentity: DescriptorIdentity;
}

async function verifyPublishedHandoff(
  directoryFd: number,
  published: PublishedHandoff,
  media: ValidatedMedia,
  canonicalKey: string,
): Promise<void> {
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

function sameEntryIdentity(left: DescriptorIdentity, right: DescriptorIdentity): boolean {
  return left.isFile && right.isFile && left.dev === right.dev && left.ino === right.ino &&
    left.uid === right.uid && left.mode === right.mode && left.size === right.size &&
    left.ctimeNs === right.ctimeNs && left.mtimeNs === right.mtimeNs;
}

async function entryIdentityAt(directoryFd: number, name: string): Promise<DescriptorIdentity> {
  const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
  try {
    return await descriptorIdentity(fd);
  } finally {
    await closeDescriptor(fd);
  }
}

async function entryIdentityAtPinned(
  directoryFd: number,
  name: string,
): Promise<DescriptorIdentity> {
  const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
  try {
    return descriptorIdentitySync(fd);
  } finally {
    await closeDescriptor(fd);
  }
}

async function requireLockIdentity(
  directoryFd: number,
  lockFd: number,
  expected: DescriptorIdentity,
): Promise<void> {
  const held = await descriptorIdentity(lockFd);
  const path = await entryIdentityAt(directoryFd, LOCK_BASENAME);
  if (!sameEntryIdentity(held, expected) || !sameEntryIdentity(path, expected)) {
    throw new Error("Handoff lock identity changed.");
  }
}

async function requireCallbackBoundary(
  directoryFd: number,
  published: PublishedHandoff,
  payloadFd: number,
  expectedPayloadIdentity: DescriptorIdentity,
  expectedRecordIdentity: DescriptorIdentity,
  expectedNames: readonly string[],
): Promise<void> {
  const payload = await entryIdentityAt(directoryFd, published.handoffFilename);
  const openedPayload = await descriptorIdentity(payloadFd);
  const record = await entryIdentityAt(
    directoryFd,
    `${published.rawIntakePublicId}${RECORD_SUFFIX}`,
  );
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

interface PublisherOptions {
  hook?: HandoffHook;
  freeBytes?: (directoryFd: number) => Promise<number>;
  lockTimeoutMs?: number;
  markUnhealthy?: () => void;
}

function sha256(bytes: Buffer | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function saveIdentity(value: DescriptorIdentity): SavedIdentity {
  return {
    dev: value.dev.toString(), ino: value.ino.toString(), uid: value.uid,
    mode: value.mode, size: value.size, ctimeNs: value.ctimeNs.toString(),
    mtimeNs: value.mtimeNs.toString(),
  };
}

function restoreIdentity(value: SavedIdentity): DescriptorIdentity {
  return {
    dev: BigInt(value.dev), ino: BigInt(value.ino), uid: value.uid,
    mode: value.mode, size: value.size, ctimeNs: BigInt(value.ctimeNs),
    mtimeNs: BigInt(value.mtimeNs), isFile: true, isDirectory: false,
  };
}

function sameSavedIdentity(left: SavedIdentity, right: SavedIdentity): boolean {
  return Object.keys(left).every((key) =>
    left[key as keyof SavedIdentity] === right[key as keyof SavedIdentity]);
}

function validateClaim(claim: ReclaimClaim): void {
  if (!PUBLIC_ID.test(claim.rawIntakePublicId) ||
      !/^fcj_[0-9a-f]{40}$/u.test(claim.jobPublicId) ||
      claim.jobPublicId !== `fcj_${sha256(`finance-capture-job-v1\0${claim.rawIntakePublicId}`).slice(0, 40)}` ||
      ![claim.canonicalKeyHash, claim.ingressIdentityDigest, claim.attachmentContentHash]
        .every((value) => /^[0-9a-f]{64}$/u.test(value))) {
    throw new Error("Reclaim claim identity is invalid.");
  }
}

function parseIntent(bytes: Buffer): ReclaimIntent {
  if (bytes.byteLength > 4_096) throw new Error("Reclaim intent exceeds its bounded size.");
  let value: unknown;
  try { value = JSON.parse(bytes.toString("utf8")); } catch (error) {
    throw new Error("Reclaim intent is invalid JSON.", { cause: error });
  }
  if (!isRecord(value) || value.schema_version !== "finance-bridge-reclaim-v1" ||
      !isRecord(value.claim) || !isRecord(value.record_identity) ||
      !isRecord(value.payload_identity) || !isRecord(value.directory_identity) ||
      typeof value.record_basename !== "string" || typeof value.payload_basename !== "string" ||
      typeof value.record_sha256 !== "string" || typeof value.payload_sha256 !== "string") {
    throw new Error("Reclaim intent fields are invalid.");
  }
  const intent = value as unknown as ReclaimIntent;
  validateClaim(intent.claim);
  if (Object.keys(value).sort().join() !== [
    "claim", "directory_identity", "payload_basename", "payload_identity",
    "payload_sha256", "record_basename", "record_identity", "record_sha256", "schema_version",
  ].sort().join() ||
      Object.keys(intent.claim).sort().join() !== [
        "rawIntakePublicId", "jobPublicId", "canonicalKeyHash", "ingressIdentityDigest",
        "attachmentContentHash",
      ].sort().join() ||
      intent.record_basename !== `${intent.claim.rawIntakePublicId}${RECORD_SUFFIX}` ||
      ![".jpg", ".png"].some((suffix) =>
        intent.payload_basename === `${intent.claim.rawIntakePublicId}${suffix}`) ||
      intent.payload_sha256 !== intent.claim.attachmentContentHash ||
      ![intent.record_sha256, intent.payload_sha256].every((hash) => /^[0-9a-f]{64}$/u.test(hash))) {
    throw new Error("Reclaim intent identity is invalid.");
  }
  for (const item of [intent.record_identity, intent.payload_identity, intent.directory_identity]) {
    if (Object.keys(item).sort().join() !== [
      "dev", "ino", "uid", "mode", "size", "ctimeNs", "mtimeNs",
    ].sort().join() ||
        ![item.dev, item.ino, item.ctimeNs, item.mtimeNs].every((part) =>
          typeof part === "string" && /^(?:0|[1-9][0-9]*)$/u.test(part)) ||
        ![item.uid, item.mode, item.size].every((part) =>
          Number.isSafeInteger(part) && part >= 0)) {
      throw new Error("Reclaim intent inode evidence is invalid.");
    }
  }
  return intent;
}

function lockOperation(fileDescriptor: number, operation: "ex" | "un"): Promise<void> {
  return new Promise((resolve, reject) => {
    flock(fileDescriptor, operation, (error) => error ? reject(error) : resolve());
  });
}

function tryExclusiveLock(fileDescriptor: number): Promise<boolean> {
  return new Promise((resolve, reject) => {
    flock(fileDescriptor, "exnb", (error) => {
      if (error === null || error === undefined) return resolve(true);
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "EAGAIN" || code === "EWOULDBLOCK") return resolve(false);
      reject(error);
    });
  });
}

async function acquireExclusiveLock(fileDescriptor: number, timeoutMs: number): Promise<void> {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > DEFAULT_LOCK_TIMEOUT_MS) {
    throw new Error("Handoff lock timeout is invalid.");
  }
  const deadline = performance.now() + timeoutMs;
  while (!await tryExclusiveLock(fileDescriptor)) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new Error("Handoff lock deadline exceeded.");
    await delay(Math.min(25, Math.ceil(remaining)));
  }
}

async function requireDirectoryPathIdentity(
  path: string,
  expected: Awaited<ReturnType<typeof descriptorIdentity>>,
): Promise<void> {
  let pathFd: number | undefined;
  try {
    pathFd = openDirectory(path);
    const actual = await descriptorIdentity(pathFd);
    if (!actual.isDirectory || actual.dev !== expected.dev || actual.ino !== expected.ino ||
        (actual.mode & 0o077) !== 0) {
      throw new Error("Handoff directory identity changed.");
    }
  } finally {
    if (pathFd !== undefined) await closeDescriptor(pathFd);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseRecord(bytes: Buffer): SlotRecord {
  if (bytes.byteLength > 4_096) throw new Error("Handoff record exceeds its bounded size.");
  let value: unknown;
  try { value = JSON.parse(bytes.toString("utf8")); } catch (error) {
    throw new Error("Handoff record is invalid JSON.", { cause: error });
  }
  if (!isRecord(value)) throw new Error("Handoff record must be an object.");
  const exact = [
    "schema_version", "raw_intake_public_id", "canonical_key_hash", "content_hash",
    "byte_size", "detected_mime_type", "canonical_extension", "payload_basename",
  ];
  if (Object.keys(value).length !== exact.length || exact.some((field) => !(field in value)) ||
      value.schema_version !== "finance-bridge-handoff-v1" ||
      typeof value.raw_intake_public_id !== "string" || !PUBLIC_ID.test(value.raw_intake_public_id) ||
      typeof value.canonical_key_hash !== "string" || !/^[0-9a-f]{64}$/u.test(value.canonical_key_hash) ||
      typeof value.content_hash !== "string" || !/^[0-9a-f]{64}$/u.test(value.content_hash) ||
      !Number.isSafeInteger(value.byte_size) || (value.byte_size as number) <= 0 ||
      !["image/jpeg", "image/png"].includes(value.detected_mime_type as string) ||
      ![".jpg", ".png"].includes(value.canonical_extension as string) ||
      typeof value.payload_basename !== "string" ||
      value.payload_basename !== `${value.raw_intake_public_id}${value.canonical_extension}`) {
    throw new Error("Handoff record fields are invalid.");
  }
  return value as unknown as SlotRecord;
}

function serializedRecord(record: SlotRecord): Buffer {
  return Buffer.from(`${JSON.stringify(record)}\n`, "utf8");
}

async function readRegularAt(directoryFd: number, name: string, maximum: number): Promise<Buffer> {
  const fd = openFileAt(directoryFd, name, constants.O_RDONLY);
  try {
    return await readDescriptor(fd, maximum);
  } finally {
    await closeDescriptor(fd);
  }
}

async function readIntentAt(directoryFd: number, name: string): Promise<ReclaimIntent> {
  return parseIntent(await readRegularAt(directoryFd, name, 4_096));
}

async function sealReclaimIntent(
  directoryFd: number,
  claim: ReclaimClaim,
  recordName: string,
  payloadName: string,
  recordIdentity: DescriptorIdentity,
  payloadIdentity: DescriptorIdentity,
): Promise<void> {
  validateClaim(claim);
  const intentName = `${claim.rawIntakePublicId}${RECLAIM_SUFFIX}`;
  const recordBytes = await readRegularAt(directoryFd, recordName, 4_096);
  const payloadBytes = await readRegularAt(directoryFd, payloadName, 10_000_000);
  if (!sameEntryIdentity(await entryIdentityAt(directoryFd, recordName), recordIdentity) ||
      !sameEntryIdentity(await entryIdentityAt(directoryFd, payloadName), payloadIdentity) ||
      sha256(payloadBytes) !== claim.attachmentContentHash ||
      parseRecord(recordBytes).canonical_key_hash !== claim.canonicalKeyHash) {
    throw new Error("Reclaim source changed before intent was sealed.");
  }
  const directoryIdentity = await descriptorIdentity(directoryFd);
  const intent: ReclaimIntent = {
    schema_version: "finance-bridge-reclaim-v1", claim,
    record_basename: recordName, payload_basename: payloadName,
    record_sha256: sha256(recordBytes), payload_sha256: sha256(payloadBytes),
    record_identity: saveIdentity(recordIdentity), payload_identity: saveIdentity(payloadIdentity),
    directory_identity: saveIdentity(directoryIdentity),
  };
  const bytes = Buffer.from(`${JSON.stringify(intent)}\n`, "utf8");
  if (bytes.length > 4_096) throw new Error("Reclaim intent exceeds its bounded size.");
  try {
    const existing = await readRegularAt(directoryFd, intentName, 4_096);
    const prior = parseIntent(existing);
    if (JSON.stringify(prior.claim) !== JSON.stringify(claim) ||
        prior.record_basename !== recordName || prior.payload_basename !== payloadName ||
        prior.record_sha256 !== intent.record_sha256 ||
        prior.payload_sha256 !== intent.payload_sha256 ||
        !sameSavedIdentity(prior.record_identity, intent.record_identity) ||
        !sameSavedIdentity(prior.payload_identity, intent.payload_identity) ||
        prior.directory_identity.dev !== intent.directory_identity.dev ||
        prior.directory_identity.ino !== intent.directory_identity.ino) {
      throw new Error("Reclaim intent conflicts with pinned slot.");
    }
    return;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const fd = openFileAt(directoryFd, intentName,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    await writeDescriptor(fd, bytes);
    await syncDescriptor(fd);
  } finally {
    await closeDescriptor(fd);
  }
  await syncDescriptor(directoryFd);
  if (!(await readRegularAt(directoryFd, intentName, 4_096)).equals(bytes)) {
    throw new Error("Reclaim intent changed after durable write.");
  }
}

async function publishPending(
  directoryFd: number,
  pendingName: string,
  finalName: string,
  bytes: Buffer,
  afterFsync: HandoffPhase,
  afterPin: HandoffPhase,
  afterPublish: HandoffPhase,
  hook?: HandoffHook,
): Promise<DescriptorIdentity> {
  let pendingMatches = false;
  let pendingExists = false;
  try {
    const existing = await readRegularAt(directoryFd, pendingName, Math.max(bytes.byteLength, 4_096));
    pendingExists = true;
    pendingMatches = existing.equals(bytes);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  if (pendingExists && !pendingMatches) {
    throw new Error("Foreign handoff pending residue blocks intake.");
  }
  if (!pendingMatches) {
    const fd = openFileAt(
      directoryFd,
      pendingName,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    try {
      await writeDescriptor(fd, bytes);
      await syncDescriptor(fd);
      await chmodDescriptor(fd, 0o600);
    } finally {
      await closeDescriptor(fd);
    }
  }
  await hook?.(afterFsync);
  const pendingBefore = await readRegularAt(directoryFd, pendingName, Math.max(bytes.byteLength, 4_096));
  if (!pendingBefore.equals(bytes)) throw new Error("Handoff pending content changed before publication.");
  try {
    renameNoReplaceAt(directoryFd, pendingName, finalName);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
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
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
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
  } finally {
    await closeDescriptor(finalFd);
  }
}

async function requirePendingCompatible(
  directoryFd: number,
  pendingName: string,
  expected: Buffer,
): Promise<boolean> {
  try {
    const pending = await readRegularAt(
      directoryFd,
      pendingName,
      Math.max(expected.byteLength, 4_096),
    );
    if (!pending.equals(expected)) {
      throw new Error("Foreign handoff pending residue blocks intake.");
    }
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    return false;
  }
}

async function defaultFreeBytes(directoryFd: number): Promise<number> {
  return descriptorFreeBytes(directoryFd);
}

async function inventory(directoryFd: number): Promise<DirectoryInventory> {
  const entries = listAt(directoryFd);
  const records = new Map<string, SlotRecord>();
  const intents = new Map<string, ReclaimIntent>();
  let treeBytes = 0;
  let payloadBytes = 0;
  const payloadNames = new Set<string>();
  for (const name of entries) {
    let fd: number;
    try {
      fd = openFileAt(directoryFd, name, constants.O_RDONLY);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ELOOP") {
        throw new Error("Handoff tree contains a symlink.", { cause: error });
      }
      throw error;
    }
    let status;
    try { status = await descriptorIdentity(fd); } finally { await closeDescriptor(fd); }
    if (!status.isFile) throw new Error("Handoff tree contains an unknown non-file entry.");
    if ((status.mode & 0o077) !== 0) throw new Error("Handoff entry permissions are not private.");
    treeBytes += status.size;
    if (name === LOCK_BASENAME || name === HANDOFF_PENDING_RECORD || name === HANDOFF_PENDING_PAYLOAD) {
      continue;
    }
    if (name.endsWith(RECORD_SUFFIX)) {
      const id = name.slice(0, -RECORD_SUFFIX.length);
      if (!PUBLIC_ID.test(id)) throw new Error("Handoff tree contains an unknown record.");
      const record = parseRecord(await readRegularAt(directoryFd, name, 4_096));
      if (record.raw_intake_public_id !== id) throw new Error("Handoff record filename mismatch.");
      records.set(id, record);
      continue;
    }
    if (name.endsWith(RECLAIM_SUFFIX)) {
      const id = name.slice(0, -RECLAIM_SUFFIX.length);
      if (!PUBLIC_ID.test(id)) throw new Error("Handoff tree contains an unknown reclaim intent.");
      const intent = await readIntentAt(directoryFd, name);
      if (intent.claim.rawIntakePublicId !== id) throw new Error("Reclaim intent filename mismatch.");
      intents.set(id, intent);
      continue;
    }
    if (/^raw_intake_bridge_[0-9a-f]{32}\.(?:jpg|png)$/u.test(name)) {
      payloadBytes += status.size;
      payloadNames.add(name);
      continue;
    }
    throw new Error("Handoff tree contains an unknown entry.");
  }
  const incompleteRecords = new Set<string>();
  const referencedPayloads = new Set<string>();
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
  for (const [id, intent] of intents) {
    const record = records.get(id);
    if (record === undefined || record.payload_basename !== intent.payload_basename ||
        record.canonical_key_hash !== intent.claim.canonicalKeyHash ||
        record.content_hash !== intent.claim.attachmentContentHash ||
        sha256(await readRegularAt(directoryFd, intent.record_basename, 4_096)) !== intent.record_sha256 ||
        !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.record_basename),
          restoreIdentity(intent.record_identity)) ||
        !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.payload_basename),
          restoreIdentity(intent.payload_identity))) {
      throw new Error("Reclaim intent does not match its retained slot.");
    }
  }
  return { recordCount: records.size, payloadBytes, treeBytes, incompleteRecords };
}

export class HandoffPublisher {
  constructor(
    private readonly workspaceRoot: string,
    private readonly options: PublisherOptions = {},
  ) {}

  private async withReclaimLock<T>(callback: (
    directoryFd: number, directoryIdentity: DescriptorIdentity,
  ) => Promise<T>, deadlineAt?: number): Promise<T | undefined> {
    const handoffPath = join(this.workspaceRoot, "handoff");
    const workspaceFd = openDirectory(this.workspaceRoot);
    let directoryFd: number | undefined;
    try {
      try { directoryFd = openExistingDirectoryAt(workspaceFd, "handoff"); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
        throw error;
      }
    } finally { await closeDescriptor(workspaceFd); }
    if (directoryFd === undefined) return undefined;
    let lockFd: number | undefined;
    try {
      const directoryIdentity = await descriptorIdentity(directoryFd);
      if (!directoryIdentity.isDirectory || directoryIdentity.uid !== process.getuid?.() ||
          (directoryIdentity.mode & 0o077) !== 0) {
        throw new Error("Reclaim handoff directory is not private.");
      }
      lockFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDWR | constants.O_NOFOLLOW);
      const lockIdentity = await descriptorIdentity(lockFd);
      if (!lockIdentity.isFile || lockIdentity.uid !== process.getuid?.() ||
          (lockIdentity.mode & 0o077) !== 0) {
        throw new Error("Reclaim handoff lock is not private.");
      }
      const remaining = deadlineAt === undefined ? DEFAULT_LOCK_TIMEOUT_MS :
        Math.min(DEFAULT_LOCK_TIMEOUT_MS, Math.ceil(deadlineAt - performance.now()));
      if (remaining <= 0) throw new Error("Reclaim deadline exceeded before lock.");
      await acquireExclusiveLock(lockFd,
        Math.min(this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS, remaining));
      try {
        await requireDirectoryPathIdentity(handoffPath, directoryIdentity);
        await requireLockIdentity(directoryFd, lockFd, lockIdentity);
        const result = await callback(directoryFd, directoryIdentity);
        await requireDirectoryPathIdentity(handoffPath, directoryIdentity);
        await requireLockIdentity(directoryFd, lockFd, lockIdentity);
        return result;
      } finally { await lockOperation(lockFd, "un"); }
    } catch (error) {
      this.options.markUnhealthy?.();
      throw error;
    } finally {
      if (lockFd !== undefined) await closeDescriptor(lockFd);
      await closeDescriptor(directoryFd);
    }
  }

  /** Lists only durable intents. The caller must query Core outside the flock. */
  async pendingReclaims(): Promise<ReclaimClaim[]> {
    return await this.withReclaimLock(async (directoryFd) => {
      const names = listAt(directoryFd);
      if (names.includes(HANDOFF_PENDING_RECORD) || names.includes(HANDOFF_PENDING_PAYLOAD)) {
        throw new Error("Unresolved handoff pending residue blocks reclaim.");
      }
      const claims: ReclaimClaim[] = [];
      for (const name of names) {
        if (!name.endsWith(RECLAIM_SUFFIX)) continue;
        const intent = await readIntentAt(directoryFd, name);
        if (name !== `${intent.claim.rawIntakePublicId}${RECLAIM_SUFFIX}`) {
          throw new Error("Reclaim intent filename mismatch.");
        }
        claims.push(intent.claim);
      }
      if (claims.length === 0) {
        const current = await inventory(directoryFd);
        if (current.incompleteRecords.size > 0) {
          throw new Error("Incomplete handoff residue blocks reclaim.");
        }
      } else {
        for (const claim of claims) {
          await this.verifyReclaimInventory(directoryFd, names,
            await readIntentAt(directoryFd, `${claim.rawIntakePublicId}${RECLAIM_SUFFIX}`));
        }
      }
      return claims;
    }) ?? [];
  }

  /** A host replay can seal an older retained slot after Core commit lost its response. */
  async prepareRetainedReclaim(canonicalKey: string, claim: ReclaimClaim): Promise<boolean> {
    validateClaim(claim);
    let found = false;
    await this.withRetained(canonicalKey, claim.rawIntakePublicId, async () => {
      found = true;
    }, this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS, claim);
    return found;
  }

  async isReclaimed(claim: ReclaimClaim): Promise<boolean> {
    validateClaim(claim);
    return await this.withReclaimLock(async (directoryFd) => {
      const names = listAt(directoryFd);
      const current = await inventory(directoryFd);
      if (current.incompleteRecords.size > 0) {
        throw new Error("Incomplete handoff residue blocks reclaim proof.");
      }
      return !names.includes(`${claim.rawIntakePublicId}${RECORD_SUFFIX}`) &&
        !names.includes(`${claim.rawIntakePublicId}.jpg`) &&
        !names.includes(`${claim.rawIntakePublicId}.png`) &&
        !names.includes(`${claim.rawIntakePublicId}${RECLAIM_SUFFIX}`);
    }) ?? true;
  }

  /** Proof is obtained from Core outside the flock before *every* cleanup attempt. */
  async reclaimVerified(
    claim: ReclaimClaim,
    proveCoreCustody: (claim: ReclaimClaim) => Promise<boolean>,
    deadlineAt?: number,
  ): Promise<boolean> {
    validateClaim(claim);
    const intentName = `${claim.rawIntakePublicId}${RECLAIM_SUFFIX}`;
    const candidate = await this.withReclaimLock(async (directoryFd) => {
      const names = listAt(directoryFd);
      if (!names.includes(intentName)) return false;
      const intent = await readIntentAt(directoryFd, intentName);
      if (JSON.stringify(intent.claim) !== JSON.stringify(claim)) {
        throw new Error("Reclaim identity conflicts with durable intent.");
      }
      return true;
    }, deadlineAt);
    if (candidate !== true) return false;
    if (deadlineAt !== undefined && performance.now() >= deadlineAt) {
      throw new Error("Reclaim deadline exceeded before Core proof.");
    }
    if (!await proveCoreCustody(claim)) return false;
    if (deadlineAt !== undefined && performance.now() >= deadlineAt) {
      throw new Error("Reclaim deadline exceeded after Core proof.");
    }
    return await this.withReclaimLock(async (directoryFd, directoryIdentity) => {
      const names = listAt(directoryFd);
      if (!names.includes(intentName)) {
        if (names.includes(`${claim.rawIntakePublicId}${RECORD_SUFFIX}`) ||
            names.some((name) => name === `${claim.rawIntakePublicId}.jpg` ||
              name === `${claim.rawIntakePublicId}.png`)) {
          throw new Error("Reclaim intent disappeared while its slot remains.");
        }
        return true; // Another process completed this verified reclaim.
      }
      const intentIdentity = await entryIdentityAtPinned(directoryFd, intentName);
      const intent = await readIntentAt(directoryFd, intentName);
      if (JSON.stringify(intent.claim) !== JSON.stringify(claim) ||
          directoryIdentity.dev.toString() !== intent.directory_identity.dev ||
          directoryIdentity.ino.toString() !== intent.directory_identity.ino) {
        throw new Error("Reclaim ticket or directory identity changed.");
      }
      await this.verifyReclaimInventory(directoryFd, names, intent);
      const payloadPresent = names.includes(intent.payload_basename);
      const recordPresent = names.includes(intent.record_basename);
      if (payloadPresent) {
        const bytes = await readRegularAt(directoryFd, intent.payload_basename, 10_000_000);
        if (sha256(bytes) !== intent.payload_sha256 ||
            !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.payload_basename),
              restoreIdentity(intent.payload_identity))) {
          throw new Error("Reclaim payload identity changed.");
        }
      }
      if (recordPresent) {
        const bytes = await readRegularAt(directoryFd, intent.record_basename, 4_096);
        if (sha256(bytes) !== intent.record_sha256 ||
            !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.record_basename),
              restoreIdentity(intent.record_identity))) {
          throw new Error("Reclaim record identity changed.");
        }
      }
      if (payloadPresent) {
        unlinkAtIfIdentity(directoryFd, intent.payload_basename,
          restoreIdentity(intent.payload_identity));
        await this.options.hook?.("after-reclaim-payload-unlink");
        await syncDescriptor(directoryFd);
        await this.options.hook?.("after-reclaim-payload-fsync");
        await this.verifyReclaimInventory(directoryFd, listAt(directoryFd), intent);
      }
      if (recordPresent) {
        unlinkAtIfIdentity(directoryFd, intent.record_basename,
          restoreIdentity(intent.record_identity));
        await this.options.hook?.("after-reclaim-record-unlink");
        await syncDescriptor(directoryFd);
        await this.options.hook?.("after-reclaim-record-fsync");
        await this.verifyReclaimInventory(directoryFd, listAt(directoryFd), intent);
      }
      unlinkAtIfIdentity(directoryFd, intentName, intentIdentity);
      await this.options.hook?.("after-reclaim-intent-unlink");
      await syncDescriptor(directoryFd);
      await this.options.hook?.("after-reclaim-intent-fsync");
      return true;
    }, deadlineAt) ?? false;
  }

  private async verifyReclaimInventory(
    directoryFd: number, names: readonly string[], target: ReclaimIntent,
  ): Promise<void> {
    const nameSet = new Set(names);
    const directoryIdentity = await descriptorIdentity(directoryFd);
    if (names.includes(HANDOFF_PENDING_RECORD) || names.includes(HANDOFF_PENDING_PAYLOAD)) {
      throw new Error("Unresolved handoff pending residue blocks reclaim.");
    }
    const records = new Map<string, SlotRecord>();
    const intents = new Map<string, ReclaimIntent>();
    const payloads = new Set<string>();
    for (const name of names) {
      if (name === LOCK_BASENAME || name.endsWith(RECLAIM_SUFFIX) &&
          /^raw_intake_bridge_[0-9a-f]{32}\.reclaim\.json$/u.test(name) ||
          /^raw_intake_bridge_[0-9a-f]{32}\.handoff\.json$/u.test(name) ||
          /^raw_intake_bridge_[0-9a-f]{32}\.(?:jpg|png)$/u.test(name)) {
        if (name.endsWith(RECORD_SUFFIX)) {
          const record = parseRecord(await readRegularAt(directoryFd, name, 4_096));
          if (name !== `${record.raw_intake_public_id}${RECORD_SUFFIX}`) {
            throw new Error("Handoff record filename mismatch.");
          }
          records.set(record.raw_intake_public_id, record);
        } else if (name.endsWith(RECLAIM_SUFFIX)) {
          const intent = await readIntentAt(directoryFd, name);
          if (name !== `${intent.claim.rawIntakePublicId}${RECLAIM_SUFFIX}`) {
            throw new Error("Reclaim intent filename mismatch.");
          }
          intents.set(intent.claim.rawIntakePublicId, intent);
        } else if (name !== LOCK_BASENAME) payloads.add(name);
        continue;
      }
      throw new Error("Handoff tree contains an unknown reclaim residue.");
    }
    if (intents.get(target.claim.rawIntakePublicId) === undefined) {
      throw new Error("Reclaim target intent disappeared.");
    }
    for (const [id, record] of records) {
      const intent = intents.get(id);
      if (payloads.has(record.payload_basename)) {
        const bytes = await readRegularAt(directoryFd, record.payload_basename, 10_000_000);
        if (bytes.length !== record.byte_size || sha256(bytes) !== record.content_hash) {
          throw new Error("Handoff payload does not match its slot record.");
        }
      } else if (intent === undefined) {
        throw new Error("Unexplained incomplete handoff slot blocks reclaim.");
      }
      if (intent !== undefined &&
          (intent.record_basename !== `${id}${RECORD_SUFFIX}` ||
            intent.payload_basename !== record.payload_basename ||
            intent.claim.canonicalKeyHash !== record.canonical_key_hash ||
            intent.claim.attachmentContentHash !== record.content_hash)) {
        throw new Error("Reclaim intent conflicts with its slot record.");
      }
    }
    for (const payload of payloads) {
      if (![...records.values()].some((record) => record.payload_basename === payload)) {
        throw new Error("Unknown orphan payload blocks reclaim.");
      }
    }
    for (const [id, intent] of intents) {
      if (intent.directory_identity.dev !== directoryIdentity.dev.toString() ||
          intent.directory_identity.ino !== directoryIdentity.ino.toString()) {
        throw new Error("Reclaim directory evidence changed.");
      }
      if (!records.has(id) && payloads.has(intent.payload_basename)) {
        throw new Error("Reclaim payload has no pinned record.");
      }
      if (records.has(id)) {
        const recordBytes = await readRegularAt(directoryFd, intent.record_basename, 4_096);
        if (sha256(recordBytes) !== intent.record_sha256 ||
            !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.record_basename),
              restoreIdentity(intent.record_identity))) {
          throw new Error("Reclaim record evidence changed.");
        }
      }
      if (payloads.has(intent.payload_basename)) {
        const payloadBytes = await readRegularAt(directoryFd, intent.payload_basename, 10_000_000);
        if (sha256(payloadBytes) !== intent.payload_sha256 ||
            !sameEntryIdentity(await entryIdentityAt(directoryFd, intent.payload_basename),
              restoreIdentity(intent.payload_identity))) {
          throw new Error("Reclaim payload evidence changed.");
        }
      }
    }
    const payload = nameSet.has(target.payload_basename);
    const record = nameSet.has(target.record_basename);
    // Only P+R+I, R+I, and I are valid crash states. A payload with no record
    // is not one of them, even when an intent survived.
    if (payload && !record) throw new Error("Reclaim payload has no pinned record.");
  }

  async publish(
    canonicalKey: string,
    rawIntakePublicId: string,
    media: ValidatedMedia,
  ): Promise<PublishedHandoff> {
    return await this.withPublished(
      canonicalKey,
      rawIntakePublicId,
      media,
      async (published) => published,
    );
  }

  async withRetained<T>(
    canonicalKey: string,
    rawIntakePublicId: string,
    callback: (
      published: PublishedHandoff,
      payloadFd: number,
      media: ValidatedMedia,
    ) => Promise<T>,
    lockTimeoutMs = this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS,
    reclaimClaim?: ReclaimClaim,
  ): Promise<T | undefined> {
    if (!PUBLIC_ID.test(rawIntakePublicId) ||
        captureIdentities(canonicalKey).rawIntakePublicId !== rawIntakePublicId) {
      throw new Error("Handoff replay identity is invalid.");
    }
    const handoffPath = join(this.workspaceRoot, "handoff");
    const workspaceFd = openDirectory(this.workspaceRoot);
    let directoryFd: number | undefined;
    try {
      try {
        directoryFd = openExistingDirectoryAt(workspaceFd, "handoff");
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
        throw error;
      }
    } finally {
      await closeDescriptor(workspaceFd);
    }
    if (directoryFd === undefined) return undefined;
    const directoryStatus = await descriptorIdentity(directoryFd);
    if (!directoryStatus.isDirectory || directoryStatus.uid !== process.getuid?.() ||
        (directoryStatus.mode & 0o077) !== 0) {
      await closeDescriptor(directoryFd);
      this.options.markUnhealthy?.();
      throw new Error("Handoff directory is not private.");
    }
    let lockFd: number | undefined;
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
        let record: SlotRecord;
        try {
          record = parseRecord(await readRegularAt(directoryFd, recordName, 4_096));
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
          throw error;
        }
        if (record.raw_intake_public_id !== rawIntakePublicId ||
            record.canonical_key_hash !== sha256(canonicalKey)) {
          throw new Error("Retained handoff identity does not match replay.");
        }
        const bytes = await readRegularAt(directoryFd, record.payload_basename, 10_000_000);
        const media: ValidatedMedia = {
          bytes,
          byteSize: record.byte_size,
          contentHash: record.content_hash,
          detectedMimeType: record.detected_mime_type,
          canonicalExtension: record.canonical_extension,
        };
        const published: PublishedHandoff = {
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
          await requireCallbackBoundary(
            directoryFd, published, payloadFd, payloadIdentity, recordIdentity, expectedNames,
          );
          let result: T | undefined;
          let callbackError: unknown;
          let callbackFailed = false;
          try { result = await callback(published, payloadFd, media); }
          catch (error) { callbackError = error; callbackFailed = true; }
          await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
          await requireLockIdentity(directoryFd, lockFd, lockIdentity);
          await requireCallbackBoundary(
            directoryFd, published, payloadFd, payloadIdentity, recordIdentity, expectedNames,
          );
          await requireDirectoryPathIdentity(handoffPath, directoryStatus);
          if (reclaimClaim !== undefined) {
            if (reclaimClaim.rawIntakePublicId !== rawIntakePublicId ||
                reclaimClaim.canonicalKeyHash !== sha256(canonicalKey) ||
                reclaimClaim.attachmentContentHash !== media.contentHash) {
              throw new Error("Retained reclaim claim does not match its slot.");
            }
            await sealReclaimIntent(directoryFd, reclaimClaim, recordName,
              published.handoffFilename, recordIdentity, payloadIdentity);
          }
          if (callbackFailed) throw callbackError;
          return result;
        } finally {
          await closeDescriptor(payloadFd);
        }
      } finally {
        const after = await descriptorIdentity(lockFd);
        let pathIdentity;
        try {
          const pathFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDONLY);
          try { pathIdentity = await descriptorIdentity(pathFd); } finally { await closeDescriptor(pathFd); }
        } catch { pathIdentity = undefined; }
        if (after.dev !== lockIdentity.dev || after.ino !== lockIdentity.ino || !after.isFile) {
          throw new Error("Handoff lock identity changed.");
        }
        if (pathIdentity === undefined || pathIdentity.dev !== lockIdentity.dev ||
            pathIdentity.ino !== lockIdentity.ino || !pathIdentity.isFile) {
          throw new Error("Handoff lock identity changed.");
        }
        await lockOperation(lockFd, "un");
      }
    } catch (error) {
      this.options.markUnhealthy?.();
      throw error;
    } finally {
      if (lockFd !== undefined) await closeDescriptor(lockFd);
      await closeDescriptor(directoryFd);
    }
  }

  async withPublished<T>(
    canonicalKey: string,
    rawIntakePublicId: string,
    media: ValidatedMedia,
    callback: (published: PublishedHandoff, payloadFd: number) => Promise<T>,
    lockTimeoutMs = this.options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS,
    reclaimClaim?: ReclaimClaim,
  ): Promise<T> {
    try {
      if (!PUBLIC_ID.test(rawIntakePublicId) || media.byteSize !== media.bytes.byteLength ||
          captureIdentities(canonicalKey).rawIntakePublicId !== rawIntakePublicId ||
          sha256(media.bytes) !== media.contentHash) {
        throw new Error("Handoff identity or content is invalid.");
      }
      const handoffPath = join(this.workspaceRoot, "handoff");
      const workspaceFd = openDirectory(this.workspaceRoot);
      let directoryFd: number | undefined;
      try {
        directoryFd = openPrivateDirectoryAt(workspaceFd, "handoff");
      } finally {
        await closeDescriptor(workspaceFd);
      }
      if (directoryFd === undefined) throw new Error("Handoff directory could not be opened.");
      const directoryStatus = await descriptorIdentity(directoryFd);
      if (!directoryStatus.isDirectory || directoryStatus.uid !== process.getuid?.() ||
          (directoryStatus.mode & 0o077) !== 0) {
        throw new Error("Handoff directory is not private.");
      }
      const lockFd = openFileAt(
        directoryFd,
        LOCK_BASENAME,
        constants.O_RDWR | constants.O_CREAT | constants.O_NOFOLLOW,
        0o600,
      );
      try {
      await chmodDescriptor(lockFd, 0o600);
      const lockIdentity = await descriptorIdentity(lockFd);
      if (!lockIdentity.isFile) throw new Error("Handoff lock is not a regular file.");
      await acquireExclusiveLock(
        lockFd,
        lockTimeoutMs,
      );
      await this.options.hook?.("after-lock");
      try {
        await requireDirectoryPathIdentity(handoffPath, directoryStatus);
        const slot = await this.publishLocked(
          directoryFd,
          handoffPath,
          canonicalKey,
          rawIntakePublicId,
          media,
        );
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
          await requireCallbackBoundary(
            directoryFd,
            published,
            payloadFd,
            slot.payloadIdentity,
            slot.recordIdentity,
            expectedNames,
          );
          let result: T | undefined;
          let callbackError: unknown;
          let callbackFailed = false;
          try { result = await callback(published, payloadFd); }
          catch (error) { callbackError = error; callbackFailed = true; }
          await verifyPublishedHandoff(directoryFd, published, media, canonicalKey);
          await requireLockIdentity(directoryFd, lockFd, lockIdentity);
          await requireCallbackBoundary(
            directoryFd,
            published,
            payloadFd,
            slot.payloadIdentity,
            slot.recordIdentity,
            expectedNames,
          );
          await requireDirectoryPathIdentity(handoffPath, directoryStatus);
          if (reclaimClaim !== undefined) {
            if (reclaimClaim.rawIntakePublicId !== rawIntakePublicId ||
                reclaimClaim.canonicalKeyHash !== sha256(canonicalKey) ||
                reclaimClaim.attachmentContentHash !== media.contentHash) {
              throw new Error("Published reclaim claim does not match its slot.");
            }
            await sealReclaimIntent(directoryFd, reclaimClaim,
              `${rawIntakePublicId}${RECORD_SUFFIX}`, published.handoffFilename,
              slot.recordIdentity, slot.payloadIdentity);
          }
          if (callbackFailed) throw callbackError;
          return result as T;
        } finally {
          await closeDescriptor(payloadFd);
        }
      } finally {
        const after = await descriptorIdentity(lockFd);
        let pathIdentity;
        try {
          const pathFd = openFileAt(directoryFd, LOCK_BASENAME, constants.O_RDONLY);
          try { pathIdentity = await descriptorIdentity(pathFd); } finally { await closeDescriptor(pathFd); }
        } catch { pathIdentity = undefined; }
        if (after.dev !== lockIdentity.dev || after.ino !== lockIdentity.ino ||
            pathIdentity === undefined || pathIdentity.dev !== lockIdentity.dev ||
            pathIdentity.ino !== lockIdentity.ino || !pathIdentity.isFile) {
          throw new Error("Handoff lock identity changed.");
        }
        await lockOperation(lockFd, "un");
      }
      } finally {
        await closeDescriptor(lockFd);
        await closeDescriptor(directoryFd);
      }
    } catch (error) {
      this.options.markUnhealthy?.();
      throw error;
    }
  }

  private async publishLocked(
    directoryFd: number,
    directory: string,
    canonicalKey: string,
    rawIntakePublicId: string,
    media: ValidatedMedia,
  ): Promise<PublishedSlot> {
    const payloadBasename = `${rawIntakePublicId}${media.canonicalExtension}`;
    const recordBasename = `${rawIntakePublicId}${RECORD_SUFFIX}`;
    const record: SlotRecord = {
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
    const recordPending = await requirePendingCompatible(
      directoryFd,
      HANDOFF_PENDING_RECORD,
      recordBytes,
    );
    const payloadPending = await requirePendingCompatible(
      directoryFd,
      HANDOFF_PENDING_PAYLOAD,
      media.bytes,
    );
    let recordExists = false;
    try {
      const existing = parseRecord(await readRegularAt(directoryFd, recordBasename, 4_096));
      recordExists = true;
      if (JSON.stringify(existing) !== JSON.stringify(record)) {
        throw new Error("Handoff slot conflict for canonical message identity.");
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
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
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
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
      if (incomplete !== rawIntakePublicId) throw new Error("Foreign handoff residue blocks intake.");
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
      : await publishPending(
        directoryFd,
        HANDOFF_PENDING_RECORD,
        recordBasename,
        recordBytes,
        "after-record-fsync",
        "after-record-pin",
        "after-record-publish",
        this.options.hook,
      );
    const payloadIdentity = payloadExists
      ? await entryIdentityAtPinned(directoryFd, payloadBasename)
      : await publishPending(
        directoryFd,
        HANDOFF_PENDING_PAYLOAD,
        payloadBasename,
        media.bytes,
        "after-payload-fsync",
        "after-payload-pin",
        "after-payload-publish",
        this.options.hook,
      );
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
