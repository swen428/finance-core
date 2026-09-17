import { execFile as execFileCallback } from "node:child_process";
import { lstat, realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import type { FinanceBridgeConfig } from "./config.js";

const execFile = promisify(execFileCallback);
const VERIFIER = fileURLToPath(
  new URL("../../scripts/verify-core-distribution.py", import.meta.url),
);

export interface CoreDistributionEvidenceV1 {
  schema: "finance-core-distribution-proof-v1";
  core_version: string;
  core_commit: string;
  manifest_sha256: string;
  wheel_sha256: string;
  api_contract_version: string;
  migration_ledger_digest: string;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Core distribution verifier returned invalid evidence.");
  }
  return value as Record<string, unknown>;
}

export async function verifyCoreDistributionV1(
  config: FinanceBridgeConfig,
): Promise<CoreDistributionEvidenceV1> {
  const metadata = await lstat(VERIFIER);
  if (!metadata.isFile() || metadata.isSymbolicLink() || await realpath(VERIFIER) !== VERIFIER) {
    throw new Error("Core distribution verifier must be the packaged regular script.");
  }
  const profile = config.agentProfileV2;
  const result = await execFile(config.pythonExecutable, [
    "-I",
    VERIFIER,
    "--root", config.coreDistributionRoot,
    "--manifest-sha256", profile.coreManifestSha256,
    "--wheel-sha256", profile.coreWheelSha256,
    "--core-version", profile.coreVersion,
    "--core-commit", profile.financeCommit,
    "--api-contract-version", profile.coreApiContractVersion,
    "--migration-ledger-digest", profile.coreMigrationLedgerDigest,
  ], {
    cwd: config.coreDistributionRoot,
    env: {
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONNOUSERSITE: "1",
      PYTHONUTF8: "1",
    },
    maxBuffer: 16_384,
    timeout: 10_000,
  });
  if (result.stderr !== "") {
    throw new Error("Core distribution verifier produced unexpected diagnostics.");
  }
  let parsed: Record<string, unknown>;
  try {
    parsed = object(JSON.parse(result.stdout));
  } catch {
    throw new Error("Core distribution verifier returned invalid evidence.");
  }
  const expected = {
    schema: "finance-core-distribution-proof-v1",
    core_version: profile.coreVersion,
    core_commit: profile.financeCommit,
    manifest_sha256: profile.coreManifestSha256,
    wheel_sha256: profile.coreWheelSha256,
    api_contract_version: profile.coreApiContractVersion,
    migration_ledger_digest: profile.coreMigrationLedgerDigest,
  } satisfies CoreDistributionEvidenceV1;
  if (Object.keys(parsed).sort().join(",") !== Object.keys(expected).sort().join(",") ||
      Object.entries(expected).some(([field, value]) => parsed[field] !== value)) {
    throw new Error("Verified Core distribution evidence does not match the runtime lock.");
  }
  return expected;
}
