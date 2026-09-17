import { constants } from "node:fs";
export interface DescriptorIdentity {
    dev: bigint;
    ino: bigint;
    uid: number;
    mode: number;
    size: number;
    ctimeNs: bigint;
    mtimeNs: bigint;
    isDirectory: boolean;
    isFile: boolean;
}
export declare function descriptorIdentity(fd: number): Promise<DescriptorIdentity>;
export declare function descriptorIdentitySync(fd: number): DescriptorIdentity;
export declare function openDirectory(path: string): number;
export declare function openPrivateDirectoryAt(parentFd: number, name: string): number;
export declare function openExistingDirectoryAt(parentFd: number, name: string): number;
export declare function openFileAt(directoryFd: number, name: string, flags: number, mode?: number): number;
export declare function closeDescriptor(fd: number): Promise<void>;
export declare function chmodDescriptor(fd: number, mode: number): Promise<void>;
export declare function syncDescriptor(fd: number): Promise<void>;
export declare function readDescriptor(fd: number, maximum: number, afterInitialIdentity?: () => void | Promise<void>): Promise<Buffer>;
export declare function writeDescriptor(fd: number, bytes: Buffer): Promise<void>;
export declare function renameNoReplaceAt(directoryFd: number, source: string, target: string): void;
export declare function listAt(directoryFd: number): string[];
export declare function freeBytes(directoryFd: number): number;
export { constants };
