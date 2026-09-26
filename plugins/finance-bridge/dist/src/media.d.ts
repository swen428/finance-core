export declare const MAX_RECEIPT_BYTES = 10000000;
export type GetMediaDirectory = () => string;
export interface ValidatedMedia {
    bytes: Buffer;
    byteSize: number;
    contentHash: string;
    detectedMimeType: "image/jpeg" | "image/png";
    canonicalExtension: ".jpg" | ".png";
    originalFilename?: string;
}
export declare class ReceiptMediaUnavailableError extends Error {
    readonly originalFilename: string | undefined;
    readonly declaredMimeType: string | undefined;
    constructor(message: string, originalFilename: string | undefined, declaredMimeType: string | undefined, options?: ErrorOptions);
}
export declare class ReceiptMediaAdapter {
    private readonly getMediaDirectory;
    private readonly readTimeoutMs;
    constructor(getMediaDirectory: GetMediaDirectory, readTimeoutMs?: number);
    /** Only the pinned trusted Finance ingress path may call this host-path reader. */
    acquireTrustedInbound(value: unknown, timeoutMs?: number): Promise<ValidatedMedia>;
    acquire(value: unknown, timeoutMs?: number): Promise<ValidatedMedia>;
}
