import { createHmac } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { join } from "node:path";

import type { FinanceBridgeConfig } from "./config.js";
import type { FinanceDeliveryMaterialV1 } from "./delivery-receipt.js";

export const FINANCE_DELIVERY_RECEIPT_PROOF_VERSION =
  "finance_delivery_receipt_proof_v1" as const;
const KEY_BYTES = 32;
const KEY_FILENAME = "delivery_receipt_signing.key";

function u16be(value: number): Buffer {
  const result = Buffer.alloc(2);
  result.writeUInt16BE(value);
  return result;
}

function u32be(value: number): Buffer {
  const result = Buffer.alloc(4);
  result.writeUInt32BE(value);
  return result;
}

function field(tag: string, value: string): Buffer {
  const tagBytes = Buffer.from(tag, "ascii");
  const valueBytes = Buffer.from(value, "utf8");
  if (tagBytes.byteLength === 0 || tagBytes.byteLength > 0xffff ||
      valueBytes.byteLength > 0xffff_ffff) {
    throw new Error("Finance delivery receipt proof field is out of range.");
  }
  return Buffer.concat([
    u16be(tagBytes.byteLength), tagBytes, u32be(valueBytes.byteLength), valueBytes,
  ]);
}

function proofMaterial(
  workspacePath: string,
  material: FinanceDeliveryMaterialV1,
): Buffer {
  return Buffer.concat([
    field("version", FINANCE_DELIVERY_RECEIPT_PROOF_VERSION),
    field("workspace_path", workspacePath),
    field("attempt_nonce", material.attemptNonce),
    field("capability", material.capability),
    field("delivery_material_version", material.deliveryMaterialVersion),
    field("delivery_material_sha256", material.deliveryMaterialSha256),
    field("provider_message_id", material.providerMessageId),
    field("receipt_token_sha256", material.receiptTokenSha256),
    field("channel", material.channel),
    field("account_id", material.accountId),
    field("conversation_id", material.conversationId),
    field("session_key", material.sessionKey),
    field("source_identity_sha256", material.sourceIdentitySha256),
  ]);
}

export function financeDeliveryReceiptProofSha256(
  signingKey: Buffer,
  workspacePath: string,
  material: FinanceDeliveryMaterialV1,
): string {
  if (signingKey.byteLength !== KEY_BYTES) {
    throw new Error("Finance delivery receipt signing key is malformed.");
  }
  return createHmac("sha256", signingKey)
    .update(proofMaterial(workspacePath, material))
    .digest("hex");
}

async function loadSigningKey(config: FinanceBridgeConfig): Promise<Buffer> {
  const runtimePath = join(config.workspaceRoot, "runtime");
  const keyPath = join(runtimePath, KEY_FILENAME);
  const runtime = await lstat(runtimePath, { bigint: true });
  if (!runtime.isDirectory() || (runtime.mode & 0o777n) !== 0o700n ||
      (typeof process.getuid === "function" && runtime.uid !== BigInt(process.getuid())) ||
      await realpath(runtimePath) !== runtimePath) {
    throw new Error("Finance delivery receipt runtime directory is unsafe.");
  }
  const before = await lstat(keyPath, { bigint: true });
  if (!before.isFile() || before.isSymbolicLink() || (before.mode & 0o777n) !== 0o600n ||
      before.size !== BigInt(KEY_BYTES) ||
      (typeof process.getuid === "function" && before.uid !== BigInt(process.getuid())) ||
      await realpath(keyPath) !== keyPath) {
    throw new Error("Finance delivery receipt signing key is unsafe.");
  }
  const noFollow = "O_NOFOLLOW" in constants ? constants.O_NOFOLLOW : 0;
  const handle = await open(keyPath, constants.O_RDONLY | noFollow);
  try {
    const opened = await handle.stat({ bigint: true });
    if (opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size ||
        opened.mode !== before.mode || opened.uid !== before.uid) {
      throw new Error("Finance delivery receipt signing key changed before read.");
    }
    const key = await handle.readFile();
    const after = await handle.stat({ bigint: true });
    if (key.byteLength !== KEY_BYTES || after.dev !== opened.dev || after.ino !== opened.ino ||
        after.size !== opened.size || after.mtimeNs !== opened.mtimeNs) {
      throw new Error("Finance delivery receipt signing key changed during read.");
    }
    return key;
  } finally {
    await handle.close();
  }
}

export interface FinanceDeliveryReceiptProofProvider {
  validate(config: FinanceBridgeConfig): Promise<void>;
  authenticate(
    config: FinanceBridgeConfig,
    material: FinanceDeliveryMaterialV1,
  ): Promise<string>;
}

export const financeDeliveryReceiptProofProvider: FinanceDeliveryReceiptProofProvider = {
  async validate(config) {
    await loadSigningKey(config);
  },
  async authenticate(config, material) {
    const key = await loadSigningKey(config);
    return financeDeliveryReceiptProofSha256(key, config.workspaceRoot, material);
  },
};
