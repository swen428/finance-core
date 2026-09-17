import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import { extname, join } from "node:path";

import {
  closeDescriptor,
  constants,
  descriptorIdentity,
  openDirectory,
  openFileAt,
  readDescriptor,
} from "./posix.js";

export const MAX_RECEIPT_BYTES = 10_000_000;
const DEFAULT_MEDIA_READ_TIMEOUT_MS = 30_000;

export type GetMediaDirectory = () => string;

export interface ValidatedMedia {
  bytes: Buffer;
  byteSize: number;
  contentHash: string;
  detectedMimeType: "image/jpeg" | "image/png";
  canonicalExtension: ".jpg" | ".png";
  originalFilename?: string;
}

export class ReceiptMediaUnavailableError extends Error {
  constructor(
    message: string,
    readonly originalFilename: string | undefined,
    readonly declaredMimeType: string | undefined,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ReceiptMediaUnavailableError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function boundedOriginalFilename(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  if (value.trim().length === 0) throw new Error("Receipt original filename is empty.");
  let characters = 0;
  for (const character of value) {
    const codePoint = character.codePointAt(0)!;
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) {
      throw new Error("Receipt original filename contains an invalid Unicode scalar.");
    }
    characters += 1;
  }
  if (characters > 255) throw new Error("Receipt original filename exceeds its bounded limit.");
  return value;
}

function singleMedia(metadata: Record<string, unknown>): {url: string; mime?: string; filename?: string} {
  if (metadata.mediaStagingPending !== undefined &&
      typeof metadata.mediaStagingPending !== "boolean") {
    throw new Error("Receipt media staging state has an invalid type.");
  }
  if (metadata.mediaStagingPending === true) throw new Error("Receipt media is still pending.");
  if (metadata.mediaPath !== undefined || metadata.mediaPaths !== undefined) {
    throw new Error("Direct media paths are refused; an opaque media://inbound ID is required.");
  }
  if (metadata.mediaUrl !== undefined && typeof metadata.mediaUrl !== "string") {
    throw new Error("Receipt media URL has an invalid type.");
  }
  if (metadata.mediaUrls !== undefined && !Array.isArray(metadata.mediaUrls)) {
    throw new Error("Receipt media URL list has an invalid type.");
  }
  const urls = Array.isArray(metadata.mediaUrls) ? metadata.mediaUrls : undefined;
  if (urls !== undefined && urls.length !== 1) throw new Error("Exactly one receipt attachment is required.");
  if (urls !== undefined && metadata.mediaUrl !== undefined) {
    throw new Error("Conflicting singular and plural receipt media URLs are refused.");
  }
  const url = urls?.[0] ?? metadata.mediaUrl;
  if (typeof url !== "string") throw new Error("Exactly one receipt media URL is required.");
  if (metadata.mediaType !== undefined && typeof metadata.mediaType !== "string") {
    throw new Error("Receipt MIME type has an invalid type.");
  }
  if (metadata.mediaTypes !== undefined && !Array.isArray(metadata.mediaTypes)) {
    throw new Error("Receipt MIME type list has an invalid type.");
  }
  const types = Array.isArray(metadata.mediaTypes) ? metadata.mediaTypes : undefined;
  if (types !== undefined && (types.length !== 1 || typeof types[0] !== "string")) {
    throw new Error("Exactly one receipt MIME type is required.");
  }
  if (types !== undefined && metadata.mediaType !== undefined) {
    throw new Error("Conflicting singular and plural receipt MIME types are refused.");
  }
  const mime = types?.[0] ?? metadata.mediaType;
  const filename = metadata.originalFilename;
  if (filename !== undefined && typeof filename !== "string") {
    throw new Error("Receipt original filename has an invalid type.");
  }
  return {
    url,
    ...(typeof mime === "string" ? { mime } : {}),
    ...(typeof filename === "string" ? { filename } : {}),
  };
}

function opaqueMediaId(url: string): string {
  const prefix = "media://inbound/";
  if (!url.startsWith(prefix)) throw new Error("Receipt media must use media://inbound/.");
  let decoded: string;
  try {
    decoded = decodeURIComponent(url.slice(prefix.length));
  } catch (error) {
    throw new Error("Receipt media ID is not valid percent encoding.", { cause: error });
  }
  if (!/^[A-Za-z0-9._-]{1,200}$/u.test(decoded) || decoded === "." || decoded === "..") {
    throw new Error("Receipt media ID is unsafe.");
  }
  return decoded;
}

function detectedType(bytes: Buffer): Pick<ValidatedMedia, "detectedMimeType" | "canonicalExtension"> {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return { detectedMimeType: "image/jpeg", canonicalExtension: ".jpg" };
  }
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (bytes.length >= png.length && bytes.subarray(0, png.length).equals(png)) {
    return { detectedMimeType: "image/png", canonicalExtension: ".png" };
  }
  throw new Error("Receipt media has unsupported magic bytes.");
}

export class ReceiptMediaAdapter {
  constructor(
    private readonly getMediaDirectory: GetMediaDirectory,
    private readonly readTimeoutMs = DEFAULT_MEDIA_READ_TIMEOUT_MS,
  ) {}

  async acquire(value: unknown, timeoutMs = this.readTimeoutMs): Promise<ValidatedMedia> {
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 ||
        timeoutMs > DEFAULT_MEDIA_READ_TIMEOUT_MS) {
      throw new Error("Receipt media read timeout is invalid.");
    }
    if (!isRecord(value)) throw new Error("Receipt attachment metadata is invalid.");
    const descriptor = singleMedia(value);
    const originalFilename = boundedOriginalFilename(descriptor.filename);
    const id = opaqueMediaId(descriptor.url);
    const startedAt = performance.now();
    const requestedInbound = join(this.getMediaDirectory(), "inbound");
    let canonicalInbound: string;
    let inboundFd: number;
    try {
      canonicalInbound = await realpath(requestedInbound);
      inboundFd = openDirectory(canonicalInbound);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new ReceiptMediaUnavailableError(
          "Receipt media is no longer available.",
          originalFilename,
          descriptor.mime,
          { cause: error },
        );
      }
      throw error;
    }
    let mediaFd: number | undefined;
    let bytes: Buffer;
    try {
      const inboundIdentity = await descriptorIdentity(inboundFd);
      if (!inboundIdentity.isDirectory || inboundIdentity.uid !== process.getuid?.() ||
          (inboundIdentity.mode & 0o077) !== 0) {
        throw new Error("Media inbound root is not a private owner-controlled directory.");
      }
      const currentInbound = await realpath(requestedInbound);
      if (currentInbound !== canonicalInbound) {
        throw new Error("Media inbound root identity changed before acquisition.");
      }
      const pathFd = openDirectory(currentInbound);
      try {
        const pathIdentity = await descriptorIdentity(pathFd);
        if (pathIdentity.dev !== inboundIdentity.dev || pathIdentity.ino !== inboundIdentity.ino ||
            pathIdentity.uid !== inboundIdentity.uid || pathIdentity.mode !== inboundIdentity.mode) {
          throw new Error("Media inbound root identity changed before acquisition.");
        }
      } finally {
        await closeDescriptor(pathFd);
      }
      try {
        mediaFd = openFileAt(inboundFd, id, constants.O_RDONLY);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") {
          throw new ReceiptMediaUnavailableError(
            "Receipt media is no longer available.",
            originalFilename,
            descriptor.mime,
            { cause: error },
          );
        }
        throw error;
      }
      bytes = await readDescriptor(mediaFd, MAX_RECEIPT_BYTES);
    } finally {
      if (mediaFd !== undefined) await closeDescriptor(mediaFd);
      await closeDescriptor(inboundFd);
    }
    if (performance.now() - startedAt > timeoutMs) {
      throw new Error("Receipt media read deadline exceeded after bounded descriptor cleanup.");
    }
    const detected = detectedType(bytes);
    if (descriptor.mime !== undefined && descriptor.mime !== detected.detectedMimeType) {
      throw new Error("Declared receipt MIME does not match magic bytes.");
    }
    if (descriptor.filename !== undefined) {
      const extension = extname(descriptor.filename).toLowerCase();
      if (extension !== detected.canonicalExtension &&
          !(detected.canonicalExtension === ".jpg" && extension === ".jpeg")) {
        throw new Error("Declared receipt extension does not match magic bytes.");
      }
    }
    return {
      bytes,
      byteSize: bytes.byteLength,
      contentHash: createHash("sha256").update(bytes).digest("hex"),
      ...detected,
      ...(originalFilename === undefined
        ? {}
        : { originalFilename }),
    };
  }
}
